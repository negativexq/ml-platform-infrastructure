// S3-only KServe storage initializer. Destination must be an empty writable volume.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awshttp "github.com/aws/aws-sdk-go-v2/aws/transport/http"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

type objectStore interface {
	ListObjectsV2(context.Context, *s3.ListObjectsV2Input, ...func(*s3.Options)) (*s3.ListObjectsV2Output, error)
	GetObject(context.Context, *s3.GetObjectInput, ...func(*s3.Options)) (*s3.GetObjectOutput, error)
}

// Reset the read deadline on every socket read, including streamed object bodies.
// A large model can take longer than S3_READ_TIMEOUT while still making progress.
type idleConn struct {
	net.Conn
	timeout time.Duration
}

func (c *idleConn) Read(p []byte) (int, error) {
	if err := c.SetReadDeadline(time.Now().Add(c.timeout)); err != nil {
		return 0, err
	}
	return c.Conn.Read(p)
}

type limits struct {
	workers, objects int
	bytes            int64
}
type object struct {
	key, name, etag string
	size            int64
}

func positiveEnv(name string, fallback int64) (int64, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}
	n, err := strconv.ParseInt(value, 10, 64)
	if err != nil || n <= 0 {
		return 0, fmt.Errorf("%s must be a positive integer", name)
	}
	return n, nil
}
func boolEnv(name string, fallback bool) (bool, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}
	v, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("invalid boolean %s", name)
	}
	return v, nil
}
func newClient(ctx context.Context) (*s3.Client, error) {
	connect, err := positiveEnv("S3_CONNECT_TIMEOUT", 15)
	if err != nil {
		return nil, err
	}
	read, err := positiveEnv("S3_READ_TIMEOUT", 30)
	if err != nil {
		return nil, err
	}
	attempts, err := positiveEnv("S3_MAX_ATTEMPTS", 3)
	if err != nil {
		return nil, err
	}
	if connect > 86400 || read > 86400 || attempts > 100 {
		return nil, errors.New("timeouts must be <=86400 seconds and attempts <=100")
	}
	verify, err := boolEnv("S3_VERIFY_SSL", true)
	if err != nil {
		return nil, err
	}
	virtual, err := boolEnv("S3_USER_VIRTUAL_BUCKET", false)
	if err != nil {
		return nil, err
	}
	accelerate, err := boolEnv("S3_USE_ACCELERATE", false)
	if err != nil {
		return nil, err
	}
	anonymous, err := boolEnv("awsAnonymousCredential", false)
	if err != nil {
		return nil, err
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	dialer := &net.Dialer{Timeout: time.Duration(connect) * time.Second}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		conn, err := dialer.DialContext(ctx, network, address)
		if err != nil {
			return nil, err
		}
		return &idleConn{conn, time.Duration(read) * time.Second}, nil
	}
	transport.ResponseHeaderTimeout = time.Duration(read) * time.Second
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: !verify} // Explicit KServe compatibility setting.
	bundle := os.Getenv("AWS_CA_BUNDLE")
	if bundle == "" && os.Getenv("CA_BUNDLE_CONFIGMAP_NAME") != "" {
		mount := os.Getenv("CA_BUNDLE_VOLUME_MOUNT_POINT")
		if mount == "" {
			mount = "/kserve-custom-ca"
		}
		bundle = filepath.Join(mount, "cabundle.crt")
	}
	if verify && bundle != "" {
		pem, err := os.ReadFile(bundle)
		if err != nil {
			return nil, fmt.Errorf("read CA bundle: %w", err)
		}
		roots := x509.NewCertPool()
		if !roots.AppendCertsFromPEM(pem) {
			return nil, errors.New("CA bundle contains no certificates")
		}
		transport.TLSClientConfig.RootCAs = roots
	}
	cfg, err := config.LoadDefaultConfig(ctx, config.WithHTTPClient(awshttp.NewBuildableClient().WithTransportOptions(func(tr *http.Transport) {
		tr.DialContext = transport.DialContext
		tr.ResponseHeaderTimeout = transport.ResponseHeaderTimeout
		tr.TLSClientConfig = transport.TLSClientConfig
	})), config.WithRetryMaxAttempts(int(attempts)))
	if err != nil {
		return nil, err
	}
	if cfg.Region == "" {
		cfg.Region = "us-east-1"
	}
	if anonymous {
		cfg.Credentials = aws.AnonymousCredentials{}
	}
	endpoint := os.Getenv("AWS_ENDPOINT_URL_S3")
	if endpoint == "" {
		endpoint = os.Getenv("AWS_ENDPOINT_URL")
	}
	if endpoint != "" {
		u, err := url.Parse(endpoint)
		if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return nil, errors.New("invalid S3 endpoint URL")
		}
	}
	return s3.NewFromConfig(cfg, func(o *s3.Options) {
		if endpoint != "" {
			o.BaseEndpoint = aws.String(endpoint)
		}
		o.UsePathStyle = !virtual && !accelerate
		o.UseAccelerate = accelerate
		if endpoint == "https://s3.amazonaws.com" && cfg.Region != "us-east-1" {
			o.UsePathStyle = false
		}
		o.ResponseChecksumValidation = aws.ResponseChecksumValidationWhenSupported
	}), nil
}
func parseURI(uri string) (string, string, error) {
	u, err := url.Parse(uri)
	if err != nil || u.Scheme != "s3" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || strings.Contains(u.Host, ":") {
		return "", "", errors.New("expected s3://bucket/key-or-prefix")
	}
	return u.Host, strings.TrimPrefix(u.Path, "/"), nil
}
func safeName(name string) error {
	if name == "" || strings.ContainsAny(name, "\\\x00") || strings.HasPrefix(name, "/") {
		return errors.New("unsafe object path")
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." {
			return errors.New("unsafe object path")
		}
	}
	return nil
}
func collect(ctx context.Context, client objectStore, bucket, prefix string, bound limits) ([]object, error) {
	var result []object
	var token *string
	seen := map[string]bool{}
	var total int64
	for {
		page, err := client.ListObjectsV2(ctx, &s3.ListObjectsV2Input{Bucket: aws.String(bucket), Prefix: aws.String(prefix), ContinuationToken: token})
		if err != nil {
			return nil, fmt.Errorf("list S3 objects: %w", err)
		}
		for _, item := range page.Contents {
			key := aws.ToString(item.Key)
			if strings.HasSuffix(key, "/") {
				continue
			}
			if !strings.HasPrefix(key, prefix) {
				return nil, errors.New("S3 returned object outside requested prefix")
			}
			name := strings.TrimPrefix(strings.TrimPrefix(key, prefix), "/")
			if key == prefix {
				name = path.Base(key)
			}
			if err := safeName(name); err != nil {
				return nil, err
			}
			size := aws.ToInt64(item.Size)
			if size < 0 || size > bound.bytes-total {
				return nil, errors.New("model exceeds S3_MAX_DOWNLOAD_BYTES")
			}
			entry := object{key, name, aws.ToString(item.ETag), size}
			// An exact object takes precedence over prefix matches, as in KServe.
			if key == prefix {
				return []object{entry}, nil
			}
			total += size
			result = append(result, entry)
			if len(result) > bound.objects {
				return nil, errors.New("model exceeds S3_MAX_OBJECTS")
			}
		}
		if !aws.ToBool(page.IsTruncated) {
			break
		}
		next := aws.ToString(page.NextContinuationToken)
		if next == "" || seen[next] {
			return nil, errors.New("invalid S3 pagination token")
		}
		seen[next] = true
		token = aws.String(next)
	}
	if len(result) == 0 {
		return nil, errors.New("no model objects found")
	}
	names := map[string]bool{}
	for _, item := range result {
		if names[item.name] {
			return nil, errors.New("duplicate model path")
		}
		names[item.name] = true
	}
	for name := range names {
		for parent := path.Dir(name); parent != "."; parent = path.Dir(parent) {
			if names[parent] {
				return nil, errors.New("model file/directory collision")
			}
		}
	}
	return result, nil
}
func download(ctx context.Context, client objectStore, bucket, stage string, item object) error {
	input := &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(item.key), ChecksumMode: types.ChecksumModeEnabled}
	if item.etag != "" {
		input.IfMatch = aws.String(item.etag)
	}
	response, err := client.GetObject(ctx, input)
	if err != nil {
		return fmt.Errorf("get S3 object: %w", err)
	}
	defer response.Body.Close()
	if response.ContentLength != nil && *response.ContentLength != item.size {
		return errors.New("object size changed since listing")
	}
	target := filepath.Join(stage, filepath.FromSlash(item.name))
	if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		return err
	}
	file, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	// Read one extra byte to detect inconsistent sizes; reaching EOF also validates SDK checksums.
	n, copyErr := io.Copy(file, io.LimitReader(response.Body, item.size+1))
	if copyErr == nil && n != item.size {
		copyErr = errors.New("object download length mismatch")
	}
	if copyErr == nil {
		copyErr = file.Sync()
	}
	closeErr := file.Close()
	if copyErr != nil {
		return copyErr
	}
	return closeErr
}
func initialize(ctx context.Context, client objectStore, uri, destination string, bound limits) error {
	bucket, prefix, err := parseURI(uri)
	if err != nil {
		return err
	}
	if bound.workers < 1 || bound.workers > 64 || bound.objects < 1 || bound.bytes < 1 || bound.bytes == math.MaxInt64 {
		return errors.New("invalid download limits (workers must be 1..64)")
	}
	if err := os.MkdirAll(destination, 0755); err != nil {
		return err
	}
	stat, err := os.Lstat(destination)
	if err != nil {
		return err
	}
	if !stat.IsDir() || stat.Mode()&os.ModeSymlink != 0 {
		return errors.New("destination must be a real directory")
	}
	existing, err := os.ReadDir(destination)
	if err != nil {
		return err
	}
	if len(existing) != 0 {
		return errors.New("destination must be empty")
	}
	items, err := collect(ctx, client, bucket, prefix, bound)
	if err != nil {
		return err
	}
	stage, err := os.MkdirTemp(destination, ".mlp-download-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	jobs := make(chan object)
	failures := make(chan error, 1)
	var workers sync.WaitGroup
	for i := 0; i < bound.workers; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for item := range jobs {
				if err := ctx.Err(); err != nil {
					return
				}
				if err := download(ctx, client, bucket, stage, item); err != nil {
					select {
					case failures <- err:
					default:
					}
					cancel()
					return
				}
			}
		}()
	}
dispatch:
	for _, item := range items {
		select {
		case jobs <- item:
		case <-ctx.Done():
			break dispatch
		}
	}
	close(jobs)
	workers.Wait()
	select {
	case err := <-failures:
		return err
	default:
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	entries, err := os.ReadDir(stage)
	if err != nil {
		return err
	}
	// A mounted emptyDir cannot itself be renamed. Publish complete top-level entries;
	// Kubernetes' init-container completion is the visibility boundary for serving.
	var published []string
	for _, entry := range entries {
		target := filepath.Join(destination, entry.Name())
		if err := os.Rename(filepath.Join(stage, entry.Name()), target); err != nil {
			for _, name := range published {
				_ = os.RemoveAll(name)
			}
			return err
		}
		published = append(published, target)
	}
	return nil
}
func run(ctx context.Context, args []string) error {
	if len(args) != 2 {
		return errors.New("expected S3 model URI and destination directory")
	}
	if _, _, err := parseURI(args[0]); err != nil {
		return err
	}
	workers, err := positiveEnv("S3_MAX_FILE_CONCURRENCY", 4)
	if err != nil {
		return err
	}
	objects, err := positiveEnv("S3_MAX_OBJECTS", 10000)
	if err != nil {
		return err
	}
	bytes, err := positiveEnv("S3_MAX_DOWNLOAD_BYTES", 20<<30)
	if err != nil {
		return err
	}
	timeout, err := positiveEnv("S3_DOWNLOAD_TIMEOUT", 1800)
	if err != nil {
		return err
	}
	if timeout > int64(math.MaxInt64)/int64(time.Second) {
		return errors.New("S3_DOWNLOAD_TIMEOUT too large")
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
	defer cancel()
	client, err := newClient(ctx)
	if err != nil {
		return err
	}
	return initialize(ctx, client, args[0], args[1], limits{int(workers), int(objects), bytes})
}
func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := run(ctx, os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "S3 initialization failed:", err)
		os.Exit(1)
	}
	fmt.Println("S3 model download complete")
}
