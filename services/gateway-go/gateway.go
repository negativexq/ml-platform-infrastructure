// Project-scoped inference gateway with shared PostgreSQL budgets.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

type apiKey struct {
	ID, ProjectID, Name, Hash string
	Principal                 *principal
	Endpoints                 []string
	Limit                     *int
	Expires, Revoked          *time.Time
}
type route struct {
	ProjectID, Project, Endpoint, Status, Kind, Protocol, URL, Ref, Model string
	Limit, MaxBodyKB, Timeout                                             int
}
type bucket struct {
	Name     string `json:"name"`
	Capacity int    `json:"capacity"`
}
type allowance struct {
	Allowed                 bool
	Limit, Remaining, Reset int
}
type repository interface {
	Key(context.Context, string) (*apiKey, error)
	Route(context.Context, string, string) (*route, error)
	Touch(context.Context, string) error
	Take(context.Context, []bucket, int) (allowance, error)
	Ready(context.Context) error
}
type gatewayError struct {
	Status        int
	Code, Message string
	Headers       map[string]string
}

func (e *gatewayError) Error() string { return e.Code }
func refusal(status int, code, message string) *gatewayError {
	return &gatewayError{Status: status, Code: code, Message: message}
}

var tokenPattern = regexp.MustCompile(`^mlp_live_([0-9a-f]{8})_([A-Za-z0-9_-]{20,100})$`)
var requestIDPattern = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,64}$`)

const cacheTTL = 5 * time.Second
const maxBody = 10 * 1024 * 1024

type cacheEntry[T any] struct {
	Value T
	At    time.Time
}
type gateway struct {
	db       repository
	identity authenticator
	client   *http.Client
	metrics  *telemetry
	slots    chan struct{}
	mu       sync.Mutex
	keys     entityCache[*apiKey]
	routes   entityCache[*route]
	touched  map[string]time.Time
	now      func() time.Time
}

func newGateway(db repository, workers int, t *telemetry) *gateway {
	if t == nil {
		t = instruments(noop.NewMeterProvider().Meter("test"))
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	transport.MaxConnsPerHost = 200
	transport.MaxIdleConns = 200
	transport.MaxIdleConnsPerHost = 50
	return &gateway{db: db, metrics: t, client: &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, slots: make(chan struct{}, workers), touched: map[string]time.Time{}, now: time.Now}
}
func (g *gateway) key(ctx context.Context, id string) (*apiKey, error) {
	return g.keys.get(ctx, id, g.now, func(op context.Context) (*apiKey, error) { return g.db.Key(op, id) })
}
func (g *gateway) route(ctx context.Context, project, endpoint string) (*route, error) {
	return g.routes.get(ctx, project+"/"+endpoint, g.now, func(op context.Context) (*route, error) { return g.db.Route(op, project, endpoint) })
}
func (g *gateway) authenticate(ctx context.Context, token string) (*apiKey, error) {
	if token == "" {
		return nil, refusal(401, "unauthenticated", "send an API key: Authorization: Bearer ...")
	}
	parsed := tokenPattern.FindStringSubmatch(token)
	if parsed == nil {
		if strings.HasPrefix(token, "mlp_live_") || g.identity == nil {
			return nil, refusal(401, "unauthenticated", "not an API key")
		}
		p, err := g.identity.Authenticate(ctx, token)
		if err != nil {
			return nil, refusal(401, "unauthenticated", "invalid identity token")
		}
		return &apiKey{Name: p.subject, Principal: p}, nil
	}
	k, err := g.key(ctx, parsed[1])
	if err != nil {
		return nil, refusal(503, "data_store_unavailable", "gateway data store unavailable")
	}
	hash := sha256.Sum256([]byte(parsed[2]))
	encoded := hex.EncodeToString(hash[:])
	if k == nil || subtle.ConstantTimeCompare([]byte(k.Hash), []byte(encoded)) != 1 {
		return nil, refusal(401, "unauthenticated", "unknown API key")
	}
	if k.Revoked != nil || (k.Expires != nil && !g.now().Before(*k.Expires)) {
		return nil, refusal(401, "unauthenticated", "this API key is revoked or expired")
	}
	return k, nil
}
func (g *gateway) authorize(ctx context.Context, k *apiKey, r *route) error {
	if k.Principal != nil {
		if k.Principal.admin {
			return nil
		}
		reader, ok := g.db.(interface {
			Invokable(context.Context, string, []string) (bool, error)
		})
		if !ok {
			return refusal(503, "data_store_unavailable", "gateway data store unavailable")
		}
		allowed, err := reader.Invokable(ctx, r.ProjectID, k.Principal.subjects())
		if err != nil {
			return refusal(503, "data_store_unavailable", "gateway data store unavailable")
		}
		if !allowed {
			return refusal(403, "forbidden", "you need the invoker role in this project")
		}
		return nil
	}
	allowed := false
	for _, e := range k.Endpoints {
		allowed = allowed || e == r.Endpoint
	}
	if k.ProjectID != r.ProjectID || !allowed {
		return refusal(403, "forbidden", "this key may not call this endpoint")
	}
	g.mu.Lock()
	last, ok := g.touched[k.ID]
	touch := !ok || g.now().Sub(last) >= 60*time.Second
	if touch {
		if len(g.touched) >= 10000 {
			g.touched = map[string]time.Time{}
		}
		g.touched[k.ID] = g.now()
	}
	g.mu.Unlock()
	if touch {
		if err := g.db.Touch(ctx, k.ID); err != nil {
			return refusal(503, "data_store_unavailable", "gateway data store unavailable")
		}
	}
	return nil
}
func requestID(req *http.Request) string {
	given := req.Header.Get("X-Request-Id")
	if requestIDPattern.MatchString(given) {
		return given
	}
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		panic("random source unavailable")
	}
	return "req_" + hex.EncodeToString(b)
}
func writeError(w http.ResponseWriter, e *gatewayError, id string) {
	for k, v := range e.Headers {
		w.Header().Set(k, v)
	}
	w.Header().Set("X-Request-Id", id)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(e.Status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{"code": e.Code, "message": e.Message, "request_id": id}})
}
func (g *gateway) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.URL.Path == "/healthz" || req.URL.Path == "/readyz" {
		if req.Method != "GET" {
			writeError(w, refusal(405, "method_not_allowed", "use POST"), requestID(req))
			return
		}
		if req.URL.Path == "/readyz" {
			ctx, cancel := context.WithTimeout(req.Context(), 3*time.Second)
			defer cancel()
			if g.db.Ready(ctx) != nil {
				w.WriteHeader(503)
				_, _ = io.WriteString(w, `{"status":"not_ready"}`)
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":"ok"}`)
		return
	}
	id := requestID(req)
	parts := strings.SplitN(strings.TrimPrefix(req.URL.Path, "/"), "/", 4)
	if len(parts) != 4 || parts[0] != "v1" || parts[1] == "" || parts[2] == "" || parts[3] == "" {
		writeError(w, refusal(404, "not_found", "use POST /v1/{project}/{endpoint}/predict"), id)
		return
	}
	if req.Method != "POST" {
		writeError(w, refusal(405, "method_not_allowed", "use POST"), id)
		return
	}
	ctx := otel.GetTextMapPropagator().Extract(req.Context(), propagation.HeaderCarrier(req.Header))
	ctx, span := otel.Tracer("mlp.gateway.go").Start(ctx, "POST /v1/{project}/{endpoint}/{operation}", trace.WithSpanKind(trace.SpanKindServer))
	defer span.End()
	req = req.WithContext(ctx)
	started := time.Now()
	status := 500
	var k *apiKey
	var r *route
	units := 0
	g.metrics.inflight.Add(ctx, 1)
	defer g.metrics.inflight.Add(ctx, -1)
	defer func() {
		g.metrics.record(ctx, k, r, status, units, time.Since(started))
		span.SetAttributes(statusAttribute(status))
	}()
	fail := func(err error) {
		var e *gatewayError
		if !errors.As(err, &e) {
			e = refusal(503, "data_store_unavailable", "gateway data store unavailable")
		}
		status = e.Status
		writeError(w, e, id)
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, req.Body, maxBody))
	if err != nil {
		fail(refusal(413, "too_large", "the body is too large"))
		return
	}
	phase := time.Now()
	scheme, token, found := strings.Cut(req.Header.Get("Authorization"), " ")
	if !found || !strings.EqualFold(scheme, "bearer") {
		token = ""
	}
	k, err = g.authenticate(ctx, strings.TrimSpace(token))
	g.metrics.phase(ctx, "auth", phase, err)
	if err != nil {
		fail(err)
		return
	}
	phase = time.Now()
	r, err = g.route(ctx, parts[1], parts[2])
	g.metrics.phase(ctx, "route", phase, err)
	if err != nil {
		fail(err)
		return
	}
	if r == nil {
		fail(refusal(404, "not_found", fmt.Sprintf("no public endpoint %s/%s", parts[1], parts[2])))
		return
	}
	phase = time.Now()
	err = g.authorize(ctx, k, r)
	g.metrics.phase(ctx, "authorize", phase, err)
	if err != nil {
		fail(err)
		return
	}
	operation := map[string]string{"http": "invoke", "v2-infer": "predict", "openai": "chat/completions"}[r.Protocol]
	if parts[3] != operation {
		fail(refusal(404, "not_found", fmt.Sprintf("this endpoint speaks %s: POST /v1/%s/%s/%s", r.Protocol, r.Project, r.Endpoint, operation)))
		return
	}
	if r.Status != "READY" {
		fail(refusal(409, "not_ready", fmt.Sprintf("endpoint is %s, not READY", r.Status)))
		return
	}
	if len(body) > r.MaxBodyKB*1024 {
		fail(refusal(413, "too_large", fmt.Sprintf("the body may be at most %d KB", r.MaxBodyKB)))
		return
	}
	reserved := 1
	llm := r.Kind == "llm" || r.Protocol == "openai"
	own := r.Limit
	if k.Limit != nil {
		own = *k.Limit
	}
	if llm {
		body, reserved, err = prepareReserveChat(body, r.Endpoint, min(own, r.Limit))
		if err != nil {
			fail(err)
			return
		}
	}
	buckets := []bucket{{"endpoint:" + r.Project + "/" + r.Endpoint, r.Limit}, {"caller:" + r.Project + "/" + k.Name + "/" + r.Endpoint, own}}
	phase = time.Now()
	queued := phase
	select {
	case g.slots <- struct{}{}:
	case <-ctx.Done():
		status = 499
		return
	}
	g.metrics.phase(ctx, "limiter.queue", queued, nil)
	g.metrics.workers.Add(ctx, 1)
	work := time.Now()
	a, err := g.db.Take(ctx, buckets, reserved)
	g.metrics.phase(ctx, "limiter.work", work, err)
	g.metrics.workers.Add(ctx, -1)
	<-g.slots
	g.metrics.phase(ctx, "limiter.total", phase, err)
	if err != nil {
		fail(refusal(503, "limit_store_unavailable", "rate-limit store unavailable"))
		return
	}
	if !a.Allowed {
		e := refusal(429, "rate_limited", fmt.Sprintf("over %d units per minute", a.Limit))
		e.Headers = map[string]string{"Retry-After": fmt.Sprint(max(1, a.Reset))}
		fail(e)
		return
	}
	var meter *tokenMeter
	complete := false
	if llm {
		units = reserved
		meter = &tokenMeter{}
		parsed, _ := parseJSON(body)
		if object, ok := parsed.(jsonObject); ok {
			stream, _ := object.get("stream")
			meter.streamed = truthy(stream)
		}
		defer func() {
			meter.finish()
			measured := meter.prompt + meter.completion
			if !complete || !meter.reported {
				measured = max(reserved, measured)
			}
			units = measured
			if measured != reserved {
				settler, ok := g.db.(interface {
					operate(context.Context, []bucket, int, string) (allowance, error)
				})
				if !ok {
					units = max(reserved, measured)
					return
				}
				settlement, cancel := context.WithTimeout(context.WithoutCancel(ctx), 6*time.Second)
				defer cancel()
				op := "refund"
				delta := reserved - measured
				if delta < 0 {
					op = "charge"
					delta = -delta
				}
				if _, e := settler.operate(settlement, buckets, delta, op); e != nil {
					units = max(reserved, measured)
				}
			}
			g.metrics.tokens(ctx, k, r, meter.prompt, meter.completion)
		}()
	}
	phase = time.Now()
	response, err := g.forward(ctx, r, body, id)
	g.metrics.phase(ctx, "upstream.headers", phase, err)
	if err != nil {
		fail(err)
		return
	}
	defer response.Body.Close()
	for name, value := range map[string]string{"X-Request-Id": id, "RateLimit-Limit": fmt.Sprint(a.Limit), "RateLimit-Remaining": fmt.Sprint(a.Remaining), "RateLimit-Reset": fmt.Sprint(a.Reset)} {
		w.Header().Set(name, value)
	}
	if r.Model != "" {
		w.Header().Set("X-MLP-Model", r.Model)
	}
	content := response.Header.Get("Content-Type")
	if content == "" {
		content = "application/json"
	}
	w.Header().Set("Content-Type", content)
	status = response.StatusCode
	w.WriteHeader(status)
	phase = time.Now()
	reader := io.Reader(response.Body)
	if meter != nil {
		reader = &meteredReader{reader: reader, meter: meter}
	}
	err = copyStream(w, reader)
	complete = err == nil
	g.metrics.phase(ctx, "stream", phase, err)
	if err != nil {
		status = 502
		panic(http.ErrAbortHandler) // Preserve incomplete-body failure after headers.
	} else if !llm && status < 500 {
		units = 1
	}
}
func copyStream(w http.ResponseWriter, body io.Reader) error {
	b := make([]byte, 32*1024)
	for {
		n, err := body.Read(b)
		if n > 0 {
			if _, writeErr := w.Write(b[:n]); writeErr != nil {
				return writeErr
			}
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
	}
}
func (g *gateway) forward(ctx context.Context, r *route, body []byte, id string) (*http.Response, error) {
	if r.URL == "" {
		return nil, refusal(502, "upstream_unavailable", "the model did not answer")
	}
	base, err := url.Parse(r.URL)
	if err != nil || base.Host == "" || (base.Scheme != "http" && base.Scheme != "https") {
		return nil, refusal(502, "upstream_unavailable", "the model did not answer")
	}
	path := "/"
	if r.Protocol == "v2-infer" {
		var payload map[string]json.RawMessage
		_ = json.Unmarshal(body, &payload)
		if _, ok := payload["instances"]; ok {
			path = "/invocations"
		} else {
			_, name, _ := strings.Cut(r.Ref, "/")
			path = "/v2/models/" + url.PathEscape(name) + "/infer"
		}
	}
	if r.Protocol == "openai" {
		path = "/openai/v1/chat/completions"
	}
	ctx, cancel := context.WithCancel(ctx)
	watch := newOperationWatch(time.Duration(r.Timeout)*time.Second, cancel)
	ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{GotConn: func(httptrace.GotConnInfo) { watch.touch() }, WroteRequest: func(httptrace.WroteRequestInfo) { watch.touch() }})
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(r.URL, "/")+path, bytes.NewReader(body))
	if err != nil {
		watch.stop()
		cancel()
		return nil, refusal(502, "upstream_unavailable", "the model did not answer")
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Request-Id", id)
	ctx, span := otel.Tracer("mlp.gateway.go").Start(ctx, "HTTP POST upstream", trace.WithSpanKind(trace.SpanKindClient))
	req = req.WithContext(ctx)
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))
	response, err := g.client.Do(req)
	if err != nil {
		watch.stop()
		cancel()
		span.End()
		if watch.expired.Load() || errors.Is(err, context.DeadlineExceeded) {
			return nil, refusal(504, "timeout", fmt.Sprintf("no answer within %ds", r.Timeout))
		}
		return nil, refusal(502, "upstream_unavailable", "the model did not answer")
	}
	response.Body = &closingBody{ReadCloser: response.Body, cancel: cancel, end: span.End, watch: watch}
	return response, nil
}

type closingBody struct {
	io.ReadCloser
	cancel context.CancelFunc
	end    func(...trace.SpanEndOption)
	watch  *operationWatch
}

func (b *closingBody) Close() error {
	b.watch.stop()
	err := b.ReadCloser.Close()
	b.cancel()
	b.end()
	return err
}
