package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type fixtureStore struct {
	mu             sync.Mutex
	key            *apiKey
	route          *route
	allowance      allowance
	err            error
	takes, touches int
}

func (f *fixtureStore) Key(context.Context, string) (*apiKey, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	if f.key == nil {
		return nil, nil
	}
	copy := *f.key
	return &copy, nil
}
func (f *fixtureStore) Route(context.Context, string, string) (*route, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	if f.route == nil {
		return nil, nil
	}
	copy := *f.route
	return &copy, nil
}
func (f *fixtureStore) Touch(context.Context, string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.touches++
	return f.err
}
func (f *fixtureStore) Take(context.Context, []bucket, int) (allowance, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.takes++
	return f.allowance, f.err
}
func (f *fixtureStore) Ready(context.Context) error { return f.err }
func fixtureGateway() (*gateway, *fixtureStore, string) {
	secret := strings.Repeat("a", 32)
	hash := sha256.Sum256([]byte(secret))
	f := &fixtureStore{key: &apiKey{ID: "deadbeef", ProjectID: "p1", Name: "caller", Hash: hex.EncodeToString(hash[:]), Endpoints: []string{"fn"}}, route: &route{ProjectID: "p1", Project: "project", Endpoint: "fn", Status: "READY", Kind: "function", Protocol: "http", Limit: 600, MaxBodyKB: 1, Timeout: 1}, allowance: allowance{Allowed: false, Limit: 600, Reset: 2}}
	return newGateway(f, 12, nil), f, "mlp_live_deadbeef_" + secret
}
func call(g *gateway, token, path, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("POST", path, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("X-Request-Id", "fixture-id")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, req)
	return w
}
func code(w *httptest.ResponseRecorder) string {
	var b struct{ Error struct{ Code string } }
	_ = json.Unmarshal(w.Body.Bytes(), &b)
	return b.Error.Code
}
func TestAuthAndAuthorization(t *testing.T) {
	for _, scenario := range []string{"missing", "invalid", "wrong-secret", "expired", "revoked", "project", "endpoint", "private", "not-ready", "wrong-operation", "oversize", "llm"} {
		t.Run(scenario, func(t *testing.T) {
			g, f, token := fixtureGateway()
			want := 401
			wantCode := "unauthenticated"
			path := "/v1/project/fn/invoke"
			body := "{}"
			switch scenario {
			case "missing":
				token = ""
			case "invalid":
				token = "not-an-api-key"
			case "wrong-secret":
				token = "mlp_live_deadbeef_" + strings.Repeat("b", 32)
			case "expired":
				now := time.Now()
				f.key.Expires = &now
			case "revoked":
				now := time.Now()
				f.key.Revoked = &now
			case "project":
				f.key.ProjectID = "another"
				want = 403
				wantCode = "forbidden"
			case "endpoint":
				f.key.Endpoints = []string{"other"}
				want = 403
				wantCode = "forbidden"
			case "private":
				f.route = nil
				want = 404
				wantCode = "not_found"
			case "not-ready":
				f.route.Status = "PENDING"
				want = 409
				wantCode = "not_ready"
			case "wrong-operation":
				path = "/v1/project/fn/predict"
				want = 404
				wantCode = "not_found"
			case "oversize":
				body = strings.Repeat("a", 1025)
				want = 413
				wantCode = "too_large"
			case "llm":
				f.route.Kind = "llm"
				want = 400
				wantCode = "invalid_request"
			}
			w := call(g, token, path, body)
			if w.Code != want || code(w) != wantCode || w.Header().Get("X-Request-Id") != "fixture-id" {
				t.Fatalf("status=%d code=%s", w.Code, code(w))
			}
			if f.takes != 0 {
				t.Fatal("refusal spent quota")
			}
		})
	}
}
func TestLimiterFailureAndDenial(t *testing.T) {
	g, f, token := fixtureGateway()
	w := call(g, token, "/v1/project/fn/invoke", "{}")
	if w.Code != 429 || code(w) != "rate_limited" || w.Header().Get("Retry-After") != "2" {
		t.Fatal(w.Body.String())
	}
	f.mu.Lock()
	f.err = errors.New("private DSN must never be returned")
	f.mu.Unlock()
	w = call(g, token, "/v1/project/fn/invoke", "{}")
	if w.Code != 503 || code(w) != "limit_store_unavailable" || strings.Contains(w.Body.String(), "DSN") {
		t.Fatal(w.Body.String())
	}
	if f.touches != 1 {
		t.Fatal("touch not throttled")
	}
}
func TestCacheBoundedRevocation(t *testing.T) {
	g, f, token := fixtureGateway()
	now := time.Now()
	g.now = func() time.Time { return now }
	if call(g, token, "/v1/project/fn/invoke", "{}").Code != 429 {
		t.Fatal("initial denial")
	}
	f.mu.Lock()
	f.key.Revoked = &now
	f.mu.Unlock()
	if call(g, token, "/v1/project/fn/invoke", "{}").Code != 429 {
		t.Fatal("cache changed before expiration")
	}
	now = now.Add(5 * time.Second)
	if call(g, token, "/v1/project/fn/invoke", "{}").Code != 401 {
		t.Fatal("revocation not applied at cache expiry")
	}
}
func TestForwardHeadersPathsAndStreaming(t *testing.T) {
	for _, v2 := range []bool{false, true} {
		t.Run(map[bool]string{false: "function", true: "model"}[v2], func(t *testing.T) {
			g, f, token := fixtureGateway()
			f.allowance = allowance{true, 600, 599, 1}
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				expected := "/"
				if v2 {
					expected = "/invocations"
				}
				if r.URL.Path != expected || r.Header.Get("Authorization") != "" || r.Header.Get("X-Request-Id") != "fixture-id" {
					t.Errorf("bad forwarding contract")
				}
				body, _ := io.ReadAll(r.Body)
				if string(body) != `{"instances":[[1]]}` {
					t.Errorf("payload changed")
				}
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = io.WriteString(w, "data: first\n\n")
				w.(http.Flusher).Flush()
				time.Sleep(40 * time.Millisecond)
				_, _ = io.WriteString(w, "data: last\n\n")
			}))
			defer upstream.Close()
			f.route.URL = upstream.URL
			if v2 {
				f.route.Kind = "model"
				f.route.Protocol = "v2-infer"
				f.route.Ref = "namespace/backend"
			}
			server := httptest.NewServer(g)
			defer server.Close()
			operation := "invoke"
			if v2 {
				operation = "predict"
			}
			req, _ := http.NewRequest("POST", server.URL+"/v1/project/fn/"+operation, strings.NewReader(`{"instances":[[1]]}`))
			req.Header.Set("Authorization", "Bearer "+token)
			req.Header.Set("X-Request-Id", "fixture-id")
			started := time.Now()
			response, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			first := make([]byte, len("data: first\n\n"))
			_, err = io.ReadFull(response.Body, first)
			if err != nil || time.Since(started) > 35*time.Millisecond {
				t.Fatal("stream was buffered")
			}
			rest, _ := io.ReadAll(response.Body)
			if string(rest) != "data: last\n\n" || response.StatusCode != 200 || response.Header.Get("RateLimit-Remaining") != "599" {
				t.Fatal("response contract")
			}
		})
	}
}
func TestCancellationReleasesLimiterSlot(t *testing.T) {
	g, _, token := fixtureGateway()
	g.slots = make(chan struct{}, 1)
	g.slots <- struct{}{}
	req := httptest.NewRequest("POST", "/v1/project/fn/invoke", strings.NewReader("{}"))
	req.Header.Set("Authorization", "Bearer "+token)
	ctx, cancel := context.WithCancel(req.Context())
	cancel()
	g.ServeHTTP(httptest.NewRecorder(), req.WithContext(ctx))
	if len(g.slots) != 1 {
		t.Fatal("canceled caller consumed slot")
	}
}

func TestIncompleteUpstreamBodyRemainsTransportFailure(t *testing.T) {
	g, f, token := fixtureGateway()
	f.allowance = allowance{true, 600, 599, 1}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "100")
		_, _ = io.WriteString(w, "short")
	}))
	defer upstream.Close()
	f.route.URL = upstream.URL
	server := httptest.NewServer(g)
	defer server.Close()
	req, _ := http.NewRequest("POST", server.URL+"/v1/project/fn/invoke", strings.NewReader("{}"))
	req.Header.Set("Authorization", "Bearer "+token)
	response, err := http.DefaultClient.Do(req)
	if err == nil {
		defer response.Body.Close()
		_, err = io.ReadAll(response.Body)
	}
	if err == nil {
		t.Fatal("incomplete upstream body became a successful response")
	}
}
