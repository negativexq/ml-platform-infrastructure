package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

func await[T any](t *testing.T, ch <-chan T) T {
	t.Helper()
	select {
	case value := <-ch:
		return value
	case <-time.After(2 * time.Second):
		t.Fatal("operation blocked by unrelated work")
		var zero T
		return zero
	}
}

type blockingStore struct {
	*fixtureStore
	entered chan struct{}
	release chan struct{}
	reads   atomic.Int64
}

func (f *blockingStore) Key(ctx context.Context, id string) (*apiKey, error) {
	if id != "slow" {
		return f.fixtureStore.Key(ctx, id)
	}
	f.reads.Add(1)
	f.entered <- struct{}{}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-f.release:
		return &apiKey{ID: id}, nil
	}
}
func TestCacheMissDoesNotBlockHitsRoutesOrTouch(t *testing.T) {
	g, f, _ := fixtureGateway()
	db := &blockingStore{fixtureStore: f, entered: make(chan struct{}, 1), release: make(chan struct{})}
	g.db = db
	key, err := g.key(context.Background(), "deadbeef")
	if err != nil {
		t.Fatal(err)
	}
	r, err := g.route(context.Background(), "project", "fn")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, e := g.key(context.Background(), "slow"); done <- e }()
	await(t, db.entered)
	defer close(db.release)
	other := make(chan error, 1)
	go func() {
		if _, e := g.key(context.Background(), "deadbeef"); e != nil {
			other <- e
			return
		}
		if _, e := g.route(context.Background(), "project", "fn"); e != nil {
			other <- e
			return
		}
		if _, e := g.route(context.Background(), "project", "another"); e != nil {
			other <- e
			return
		}
		other <- g.authorize(context.Background(), key, r)
	}()
	if err := await(t, other); err != nil {
		t.Fatal(err)
	}
}
func TestCacheSharedFillSurvivesCanceledWaiter(t *testing.T) {
	g, f, _ := fixtureGateway()
	db := &blockingStore{fixtureStore: f, entered: make(chan struct{}, 1), release: make(chan struct{})}
	g.db = db
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { _, e := g.key(ctx, "slow"); first <- e }()
	await(t, db.entered)
	second := make(chan error, 1)
	go func() { _, e := g.key(context.Background(), "slow"); second <- e }()
	cancel()
	if err := await(t, first); err != context.Canceled {
		t.Fatalf("waiter ignored cancellation: %v", err)
	}
	close(db.release)
	if err := await(t, second); err != nil {
		t.Fatal(err)
	}
	if db.reads.Load() != 1 {
		t.Fatal("duplicate same-key DB reads")
	}
}
func TestCacheSlowReadDoesNotExtendTTL(t *testing.T) {
	var cache entityCache[*apiKey]
	var clock atomic.Int64
	clock.Store(time.Now().UnixNano())
	now := func() time.Time { return time.Unix(0, clock.Load()) }
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	var reads atomic.Int64
	loader := func(context.Context) (*apiKey, error) {
		if reads.Add(1) == 1 {
			entered <- struct{}{}
			<-release
		}
		return nil, nil
	}
	done := make(chan error, 1)
	go func() { _, e := cache.get(context.Background(), "missing", now, loader); done <- e }()
	await(t, entered)
	clock.Add(int64(4 * time.Second))
	close(release)
	if e := await(t, done); e != nil {
		t.Fatal(e)
	}
	if _, e := cache.get(context.Background(), "missing", now, loader); e != nil {
		t.Fatal(e)
	}
	if reads.Load() != 1 {
		t.Fatal("negative cache did not retain TTL")
	}
	clock.Add(int64(2 * time.Second))
	if _, e := cache.get(context.Background(), "missing", now, loader); e != nil {
		t.Fatal(e)
	}
	if reads.Load() != 2 {
		t.Fatal("slow fill extended stale snapshot TTL")
	}
}

func signIdentity(t *testing.T, key *rsa.PrivateKey, issuer, kid string) string {
	t.Helper()
	token := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{"iss": issuer, "sub": "alice", "aud": "mlp", "iat": time.Now().Unix(), "exp": time.Now().Add(time.Minute).Unix()})
	token.Header["kid"] = kid
	raw, e := token.SignedString(key)
	if e != nil {
		t.Fatal(e)
	}
	return raw
}
func jwk(key *rsa.PrivateKey, kid string) any {
	return map[string]string{"kty": "RSA", "kid": kid, "n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()), "e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes())}
}
func TestOIDCCachedValidationDoesNotWaitForJWKS(t *testing.T) {
	oldKey, _ := rsa.GenerateKey(rand.Reader, 2048)
	newKey, _ := rsa.GenerateKey(rand.Reader, 2048)
	var issuer string
	var requests atomic.Int64
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	var block, failed atomic.Bool
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/keys" {
			requests.Add(1)
			if failed.Load() {
				w.WriteHeader(http.StatusServiceUnavailable)
				return
			}
			if block.Load() {
				entered <- struct{}{}
				select {
				case <-release:
				case <-r.Context().Done():
					return
				}
			}
			json.NewEncoder(w).Encode(map[string]any{"keys": []any{jwk(oldKey, "old"), jwk(newKey, "new")}})
		} else {
			json.NewEncoder(w).Encode(map[string]string{"issuer": issuer, "jwks_uri": issuer + "/keys"})
		}
	}))
	defer provider.Close()
	issuer = provider.URL
	auth, _ := newOIDC(issuer, "mlp", "", "", nil)
	known := signIdentity(t, oldKey, issuer, "old")
	if _, err := auth.Authenticate(context.Background(), known); err != nil {
		t.Fatal(err)
	}
	// Force a genuine unknown-kid refresh, retaining the old fresh snapshot.
	auth.mu.Lock()
	auth.state.keys = auth.state.keys[:1]
	auth.mu.Unlock()
	block.Store(true)
	rotated := signIdentity(t, newKey, issuer, "new")
	refresh := make(chan error, 1)
	go func() { _, e := auth.Authenticate(context.Background(), rotated); refresh <- e }()
	await(t, entered)
	var once sync.Once
	defer once.Do(func() { close(release) })
	cached := make(chan error, 32)
	for i := 0; i < 32; i++ {
		go func() { _, e := auth.Authenticate(context.Background(), known); cached <- e }()
	}
	for i := 0; i < 32; i++ {
		if e := await(t, cached); e != nil {
			t.Fatal(e)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	if _, e := auth.Authenticate(ctx, rotated); e == nil {
		t.Fatal("canceled JWKS waiter accepted")
	}
	if time.Since(start) > time.Second {
		t.Fatal("JWKS waiter ignored its deadline")
	}

	once.Do(func() { close(release) })
	if e := await(t, refresh); e != nil {
		t.Fatal(e)
	}
	if requests.Load() != 2 {
		t.Fatal("JWKS refresh duplicated")
	}
	failed.Store(true)
	auth.mu.Lock()
	auth.lastUnknownRefresh = time.Time{}
	auth.mu.Unlock()
	missing := signIdentity(t, oldKey, issuer, "absent")
	if _, e := auth.Authenticate(context.Background(), missing); e == nil {
		t.Fatal("failed refresh accepted unknown key")
	}
	if _, e := auth.Authenticate(context.Background(), known); e != nil {
		t.Fatal("failed refresh invalidated fresh cached key")
	}
	auth.mu.Lock()
	auth.state.fetched = time.Now().Add(-2 * time.Hour)
	auth.mu.Unlock()
	if _, e := auth.Authenticate(context.Background(), known); e == nil {
		t.Fatal("expired cached key bypassed failed refresh")
	}
}
func TestOIDCDiscoveryCoalescesAndWaiterCanCancel(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	var issuer string
	var discovery, jwksRequests atomic.Int64
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/keys" {
			jwksRequests.Add(1)
			json.NewEncoder(w).Encode(map[string]any{"keys": []any{jwk(key, "one")}})
		} else {
			discovery.Add(1)
			entered <- struct{}{}
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			json.NewEncoder(w).Encode(map[string]string{"issuer": issuer, "jwks_uri": issuer + "/keys"})
		}
	}))
	defer func() { once.Do(func() { close(release) }); provider.Close() }()
	issuer = provider.URL
	auth, _ := newOIDC(issuer, "mlp", "", "", nil)
	token := signIdentity(t, key, issuer, "one")
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { _, e := auth.Authenticate(ctx, token); first <- e }()
	await(t, entered)
	others := make(chan error, 32)
	for i := 0; i < 32; i++ {
		go func() { _, e := auth.Authenticate(context.Background(), token); others <- e }()
	}
	cancel()
	if e := await(t, first); e != context.Canceled {
		t.Fatal("metadata waiter ignored cancellation")
	}
	once.Do(func() { close(release) })
	for i := 0; i < 32; i++ {
		if e := await(t, others); e != nil {
			t.Fatal(e)
		}
	}
	if discovery.Load() != 1 || jwksRequests.Load() != 1 {
		t.Fatal("cold provider calls not coalesced")
	}
}
func TestPublicIdleTimeoutConfiguration(t *testing.T) {
	for _, raw := range []string{"0s", "-1s", "nonsense", "601s"} {
		t.Setenv("CP_GATEWAY_IDLE_TIMEOUT", raw)
		if _, e := idleTimeoutSetting(); e == nil {
			t.Fatalf("invalid timeout accepted: %s", raw)
		}
	}
	t.Setenv("CP_GATEWAY_IDLE_TIMEOUT", "17s")
	d, e := idleTimeoutSetting()
	if e != nil || d != 17*time.Second {
		t.Fatal("timeout override ignored")
	}
	t.Setenv("CP_GATEWAY_IDLE_TIMEOUT", "")
	d, e = idleTimeoutSetting()
	if e != nil {
		t.Fatal(e)
	}
	// A real standard Go client reuses its connection after the former five-second boundary.
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) }))
	server.Config = publicServer(server.Config.Handler, d)
	server.Start()
	defer server.Close()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 2 * time.Second}
	reused := false
	call := func() {
		req, _ := http.NewRequest("GET", server.URL, nil)
		req = req.WithContext(httptrace.WithClientTrace(req.Context(), &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) { reused = info.Reused }}))
		res, e := client.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		io.Copy(io.Discard, res.Body)
		res.Body.Close()
	}
	call()
	time.Sleep(5200 * time.Millisecond)
	call()
	if !reused {
		t.Fatal("connection closed at former five-second boundary")
	}
}
