package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/pem"
	"fmt"
	"hash/crc32"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

var testLimits = limits{2, 100, 1024}

type fakeStore struct {
	items        []types.Object
	body         string
	failure      error
	delay        bool
	active, peak atomic.Int32
}

func (f *fakeStore) ListObjectsV2(ctx context.Context, in *s3.ListObjectsV2Input, opts ...func(*s3.Options)) (*s3.ListObjectsV2Output, error) {
	return &s3.ListObjectsV2Output{Contents: f.items}, nil
}
func (f *fakeStore) GetObject(ctx context.Context, in *s3.GetObjectInput, opts ...func(*s3.Options)) (*s3.GetObjectOutput, error) {
	n := f.active.Add(1)
	defer f.active.Add(-1)
	for peak := f.peak.Load(); n > peak && !f.peak.CompareAndSwap(peak, n); peak = f.peak.Load() {
	}
	if f.delay {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
	if f.failure != nil {
		return nil, f.failure
	}
	return &s3.GetObjectOutput{Body: io.NopCloser(strings.NewReader(f.body))}, nil
}
func obj(key string, size int64) types.Object {
	return types.Object{Key: aws.String(key), Size: aws.Int64(size), ETag: aws.String(`"etag"`)}
}
func TestDownloadSafety(t *testing.T) {
	cases := []struct {
		name  string
		items []types.Object
		body  string
		bound limits
		fail  bool
	}{
		{"nested", []types.Object{obj("model/MLmodel", 3), obj("model/data/model", 3)}, "abc", testLimits, false},
		{"zero-byte", []types.Object{obj("model/empty", 0)}, "", testLimits, false},
		{"traversal", []types.Object{obj("model/../escape", 3)}, "abc", testLimits, true},
		{"backslash", []types.Object{obj("model/a\\b", 3)}, "abc", testLimits, true},
		{"duplicate", []types.Object{obj("model/file", 3), obj("model/file", 3)}, "abc", testLimits, true},
		{"collision", []types.Object{obj("model/file", 3), obj("model/file/nested", 3)}, "abc", testLimits, true},
		{"empty", nil, "", testLimits, true},
		{"truncated-body", []types.Object{obj("model/file", 4)}, "abc", testLimits, true},
		{"oversize-body", []types.Object{obj("model/file", 2)}, "abc", testLimits, true},
		{"size-limit", []types.Object{obj("model/file", 3)}, "abc", limits{2, 100, 2}, true},
		{"count-limit", []types.Object{obj("model/a", 3), obj("model/b", 3)}, "abc", limits{2, 1, 1024}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dest := t.TempDir()
			store := &fakeStore{items: tc.items, body: tc.body}
			err := initialize(context.Background(), store, "s3://bucket/model/", dest, tc.bound)
			if (err != nil) != tc.fail {
				t.Fatalf("error = %v", err)
			}
			entries, _ := os.ReadDir(dest)
			if tc.fail && len(entries) != 0 {
				t.Fatalf("failed download published files: %v", entries)
			}
			if !tc.fail {
				for _, item := range tc.items {
					data, err := os.ReadFile(filepath.Join(dest, strings.TrimPrefix(*item.Key, "model/")))
					if err != nil || string(data) != tc.body {
						t.Fatalf("content: %q, %v", data, err)
					}
				}
				for _, entry := range entries {
					if strings.HasPrefix(entry.Name(), ".mlp-download-") {
						t.Fatal("staging leaked")
					}
				}
			}
		})
	}
}
func TestExactObjectAndBoundedConcurrency(t *testing.T) {
	store := &fakeStore{items: []types.Object{obj("model/file", 3), obj("model/file/other", 3)}, body: "abc"}
	dest := t.TempDir()
	if err := initialize(context.Background(), store, "s3://bucket/model/file", dest, testLimits); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dest, "file")); err != nil {
		t.Fatal(err)
	}
	store.items = nil
	store.delay = true
	for i := 0; i < 20; i++ {
		store.items = append(store.items, obj(fmt.Sprintf("model/%d", i), 3))
	}
	if err := initialize(context.Background(), store, "s3://bucket/model/", t.TempDir(), testLimits); err != nil {
		t.Fatal(err)
	}
	if store.peak.Load() > 2 || store.peak.Load() < 2 {
		t.Fatalf("concurrency = %d", store.peak.Load())
	}
}
func TestFailureAndCancellation(t *testing.T) {
	for _, cancelled := range []bool{false, true} {
		store := &fakeStore{items: []types.Object{obj("model/file", 3)}, failure: fmt.Errorf("denied")}
		ctx, cancel := context.WithCancel(context.Background())
		if cancelled {
			cancel()
		}
		defer cancel()
		dest := t.TempDir()
		if err := initialize(ctx, store, "s3://bucket/model/", dest, testLimits); err == nil {
			t.Fatal("expected failure")
		}
		entries, _ := os.ReadDir(dest)
		if len(entries) != 0 {
			t.Fatal("partial files remained")
		}
	}
	dest := t.TempDir()
	os.WriteFile(filepath.Join(dest, "existing"), []byte("keep"), 0644)
	if err := initialize(context.Background(), &fakeStore{}, "s3://bucket/model/", dest, testLimits); err == nil {
		t.Fatal("accepted nonempty destination")
	}
	link := filepath.Join(t.TempDir(), "link")
	os.Symlink(t.TempDir(), link)
	if err := initialize(context.Background(), &fakeStore{}, "s3://bucket/model/", link, testLimits); err == nil {
		t.Fatal("accepted symlink destination")
	}
}
func TestSDKPaginationAuthAndChecksum(t *testing.T) {
	for _, corrupt := range []bool{false, true} {
		t.Run(fmt.Sprint(corrupt), func(t *testing.T) {
			var lists atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if !strings.Contains(r.Header.Get("Authorization"), "Credential=test-access/") || r.Header.Get("X-Amz-Security-Token") != "test-token" {
					t.Error("credential/session-token contract lost")
				}
				if r.URL.Path == "/bucket" && r.URL.Query().Get("list-type") == "2" {
					lists.Add(1)
					w.Header().Set("Content-Type", "application/xml")
					if r.URL.Query().Get("continuation-token") == "next" {
						fmt.Fprint(w, `<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>model/data/file</Key><Size>3</Size><ETag>&quot;etag&quot;</ETag></Contents></ListBucketResult>`)
					} else {
						if r.URL.Query().Get("prefix") != "model/" {
							t.Error("prefix lost")
						}
						fmt.Fprint(w, `<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>next</NextContinuationToken><Contents><Key>model/MLmodel</Key><Size>3</Size><ETag>&quot;etag&quot;</ETag></Contents></ListBucketResult>`)
					}
					return
				}
				if r.Header.Get("If-Match") != `"etag"` || r.Header.Get("X-Amz-Checksum-Mode") != "ENABLED" {
					t.Error("integrity request missing")
				}
				data := []byte("abc")
				sum := crc32.ChecksumIEEE(data)
				if corrupt {
					sum++
				}
				raw := make([]byte, 4)
				binary.BigEndian.PutUint32(raw, sum)
				w.Header().Set("X-Amz-Checksum-Crc32", base64.StdEncoding.EncodeToString(raw))
				w.Header().Set("Content-Length", "3")
				io.Copy(w, bytes.NewReader(data))
			}))
			defer server.Close()
			for k, v := range map[string]string{"AWS_ENDPOINT_URL": server.URL, "AWS_ENDPOINT_URL_S3": "", "AWS_ACCESS_KEY_ID": "test-access", "AWS_SECRET_ACCESS_KEY": "test-secret", "AWS_SESSION_TOKEN": "test-token", "AWS_DEFAULT_REGION": "us-east-1", "AWS_CA_BUNDLE": "", "CA_BUNDLE_CONFIGMAP_NAME": "", "S3_VERIFY_SSL": "true", "S3_USER_VIRTUAL_BUCKET": "false", "S3_USE_ACCELERATE": "false", "awsAnonymousCredential": "false"} {
				t.Setenv(k, v)
			}
			client, err := newClient(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			dest := t.TempDir()
			err = initialize(context.Background(), client, "s3://bucket/model/", dest, testLimits)
			if (err != nil) != corrupt {
				t.Fatalf("checksum validation error = %v", err)
			}
			if lists.Load() != 2 {
				t.Fatalf("pages = %d", lists.Load())
			}
			if corrupt {
				entries, _ := os.ReadDir(dest)
				if len(entries) != 0 {
					t.Fatal("checksum failure published model")
				}
			}
		})
	}
}
func TestURIAndEnvironmentValidation(t *testing.T) {
	for _, uri := range []string{"https://bucket/key", "s3:///key", "s3://user:pass@bucket/key", "s3://bucket/key?token=x", "s3://bucket/key#x"} {
		if _, _, err := parseURI(uri); err == nil {
			t.Fatalf("accepted %s", uri)
		}
	}
	t.Setenv("S3_MAX_FILE_CONCURRENCY", "0")
	if err := run(context.Background(), []string{"s3://bucket/model/", t.TempDir()}); err == nil {
		t.Fatal("accepted zero concurrency")
	}
}

func TestTLSCustomCAAndAnonymous(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Error("anonymous request signed")
		}
		fmt.Fprint(w, `<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>`)
	}))
	defer server.Close()
	bundle := filepath.Join(t.TempDir(), "ca.pem")
	pemData := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})
	if err := os.WriteFile(bundle, pemData, 0644); err != nil {
		t.Fatal(err)
	}
	for k, v := range map[string]string{"AWS_ENDPOINT_URL": server.URL, "AWS_ENDPOINT_URL_S3": "", "AWS_DEFAULT_REGION": "us-east-1", "AWS_CA_BUNDLE": bundle, "S3_VERIFY_SSL": "true", "awsAnonymousCredential": "true", "S3_USER_VIRTUAL_BUCKET": "false", "S3_USE_ACCELERATE": "false"} {
		t.Setenv(k, v)
	}
	client, err := newClient(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.ListObjectsV2(context.Background(), &s3.ListObjectsV2Input{Bucket: aws.String("bucket")}); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AWS_CA_BUNDLE", "")
	t.Setenv("CA_BUNDLE_CONFIGMAP_NAME", "custom-ca")
	t.Setenv("CA_BUNDLE_VOLUME_MOUNT_POINT", filepath.Dir(bundle))
	os.WriteFile(filepath.Join(filepath.Dir(bundle), "cabundle.crt"), pemData, 0644)
	client, err = newClient(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.ListObjectsV2(context.Background(), &s3.ListObjectsV2Input{Bucket: aws.String("bucket")}); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CA_BUNDLE_CONFIGMAP_NAME", "")
	t.Setenv("S3_VERIFY_SSL", "0")
	client, err = newClient(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.ListObjectsV2(context.Background(), &s3.ListObjectsV2Input{Bucket: aws.String("bucket")}); err != nil {
		t.Fatal(err)
	}
}
