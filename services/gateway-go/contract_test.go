package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"github.com/golang-jwt/jwt/v5"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestOIDCValidation(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	var issuer string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "well-known") {
			json.NewEncoder(w).Encode(map[string]string{"issuer": issuer, "jwks_uri": issuer + "/keys"})
		} else {
			json.NewEncoder(w).Encode(map[string]any{"keys": []any{map[string]string{"kty": "RSA", "kid": "one", "n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()), "e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes())}}})
		}
	}))
	defer server.Close()
	issuer = server.URL
	auth, _ := newOIDC(issuer, "mlp", "", "", []string{"group:ops"})
	for _, kind := range []string{"valid", "audience", "issuer", "expired", "missing-iat", "future-iat", "hs256", "missing-sub"} {
		t.Run(kind, func(t *testing.T) {
			c := jwt.MapClaims{"iss": issuer, "sub": "alice", "aud": "mlp", "iat": time.Now().Unix(), "exp": time.Now().Add(time.Minute).Unix(), "groups": []string{"/ops"}}
			switch kind {
			case "audience":
				c["aud"] = "other"
			case "issuer":
				c["iss"] = "other"
			case "expired":
				c["exp"] = time.Now().Add(-time.Minute).Unix()
			case "missing-iat":
				delete(c, "iat")
			case "future-iat":
				c["iat"] = time.Now().Add(time.Minute).Unix()
			case "missing-sub":
				delete(c, "sub")
			}
			method := jwt.SigningMethodRS256
			token := jwt.NewWithClaims(method, c)
			token.Header["kid"] = "one"
			raw, _ := token.SignedString(key)
			if kind == "hs256" {
				raw, _ = jwt.NewWithClaims(jwt.SigningMethodHS256, c).SignedString([]byte("evil"))
			}
			p, e := auth.Authenticate(context.Background(), raw)
			if kind == "valid" {
				if e != nil || !p.admin || !strings.HasPrefix(p.subject, "user:oidc-") {
					t.Fatal("identity contract failed", e)
				}
			} else if e == nil {
				t.Fatal("invalid token accepted")
			}
		})
	}
}

type settlementFixture struct {
	*fixtureStore
	op      string
	delta   int
	allowed bool
}

func (f *settlementFixture) operate(_ context.Context, _ []bucket, n int, op string) (allowance, error) {
	f.op = op
	f.delta = n
	return allowance{}, nil
}
func (f *settlementFixture) Invokable(context.Context, string, []string) (bool, error) {
	return f.allowed, nil
}
func TestLLMSettlement(t *testing.T) {
	for _, usage := range []bool{true, false} {
		t.Run(map[bool]string{true: "reported", false: "missing"}[usage], func(t *testing.T) {
			g, f, token := fixtureGateway()
			sf := &settlementFixture{fixtureStore: f}
			g.db = sf
			f.allowance.Allowed = true
			f.route.Kind = "llm"
			f.route.Protocol = "openai"
			f.route.Limit = 1000
			up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/openai/v1/chat/completions" {
					t.Error("wrong protocol")
				}
				var b map[string]any
				json.NewDecoder(r.Body).Decode(&b)
				if b["model"] != "fn" {
					t.Error("model override missing")
				}
				if usage {
					w.Write([]byte(`{"usage":{"prompt_tokens":2,"completion_tokens":3}}`))
				} else {
					w.Write([]byte(`{"choices":[]}`))
				}
			}))
			defer up.Close()
			f.route.URL = up.URL
			w := call(g, token, "/v1/project/fn/chat/completions", `{"messages":[{"role":"user","content":"hello"}],"max_tokens":10}`)
			if w.Code != 200 {
				t.Fatalf("status %d", w.Code)
			}
			if usage {
				if sf.op != "refund" || sf.delta <= 0 {
					t.Fatal("usage not settled")
				}
			} else if sf.op != "" {
				t.Fatal("missing usage refunded")
			}
		})
	}
}
func TestStreamingPerReadDeadline(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for i := 0; i < 6; i++ {
			w.Write([]byte("data: hello\n\n"))
			w.(http.Flusher).Flush()
			time.Sleep(220 * time.Millisecond)
		}
	}))
	defer up.Close()
	g, f, token := fixtureGateway()
	f.allowance.Allowed = true
	f.route.URL = up.URL
	w := call(g, token, "/v1/project/fn/invoke", "{}")
	if w.Code != 200 || strings.Count(w.Body.String(), "hello") != 6 {
		t.Fatal("active stream truncated")
	}
}
func TestMeterInvalidUsage(t *testing.T) {
	for _, raw := range []string{`{"usage":{"prompt_tokens":true,"completion_tokens":2}}`, `{"usage":{"prompt_tokens":2.0,"completion_tokens":2}}`, `{"usage":{"prompt_tokens":-1,"completion_tokens":2}}`} {
		m := &tokenMeter{}
		m.feed([]byte(raw))
		m.finish()
		if m.reported {
			t.Fatal("invalid usage accepted")
		}
	}
}

func TestPythonChatVectors(t *testing.T) {
	data, e := os.ReadFile("testdata/llm-python-vectors.json")
	if e != nil {
		t.Fatal(e)
	}
	var rows []struct {
		Body            string
		Limit, Reserved int
		Prepared        any
		Error           string
	}
	if e = json.Unmarshal(data, &rows); e != nil {
		t.Fatal(e)
	}
	for i, row := range rows {
		prepared, reserved, e := prepareReserveChat([]byte(row.Body), "fn", row.Limit)
		if row.Error != "" {
			if e == nil || e.(*gatewayError).Message != row.Error {
				t.Fatalf("vector %d error mismatch: %v", i, e)
			}
			continue
		}
		if e != nil || reserved != row.Reserved {
			t.Fatalf("vector %d reservation got %d want %d error %v", i, reserved, row.Reserved, e)
		}
		var decoded any
		json.Unmarshal(prepared, &decoded)
		if !reflect.DeepEqual(decoded, row.Prepared) {
			t.Fatalf("vector %d payload mismatch", i)
		}
	}
}

type fixedIdentity struct{ p *principal }

func (f fixedIdentity) Authenticate(context.Context, string) (*principal, error) { return f.p, nil }
func TestOIDCMembershipAuthorization(t *testing.T) {
	for _, kind := range []string{"member", "outsider", "admin", "db-down"} {
		t.Run(kind, func(t *testing.T) {
			g, f, _ := fixtureGateway()
			sf := &settlementFixture{fixtureStore: f, allowed: kind == "member"}
			g.db = sf
			g.identity = fixedIdentity{&principal{subject: "user:oidc-alice", admin: kind == "admin"}}
			if kind == "db-down" {
				f.err = fmt.Errorf("private-dsn")
			}
			w := call(g, "signed-identity-token", "/v1/project/fn/invoke", "{}")
			want := 429
			if kind == "outsider" {
				want = 403
			}
			if kind == "db-down" {
				want = 503
			}
			if w.Code != want || strings.Contains(w.Body.String(), "private-dsn") {
				t.Fatalf("status %d", w.Code)
			}
			if f.touches != 0 {
				t.Fatal("OIDC touched API key")
			}
		})
	}
}
func TestLLMMeterSSEAndOverflow(t *testing.T) {
	m := &tokenMeter{streamed: true}
	for _, part := range []string{"data: {\"usage\":{\"prompt_tokens\":4,", "\"completion_tokens\":5}}\n", "data: [DONE]\n"} {
		m.feed([]byte(part))
	}
	m.finish()
	if !m.reported || m.prompt+m.completion != 9 {
		t.Fatal("SSE usage lost")
	}
	m = &tokenMeter{}
	m.feed(make([]byte, maxMeteredBytes+1))
	m.feed([]byte(`{"usage":{"prompt_tokens":2,"completion_tokens":3}}`))
	m.finish()
	if m.reported {
		t.Fatal("overflow accepted usage")
	}
}
func TestHeadersTimeout(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(1300 * time.Millisecond)
		w.Write([]byte("{}"))
	}))
	defer up.Close()
	g, f, token := fixtureGateway()
	f.allowance.Allowed = true
	f.route.URL = up.URL
	w := call(g, token, "/v1/project/fn/invoke", "{}")
	if w.Code != 504 || code(w) != "timeout" {
		t.Fatal("upstream timeout failed")
	}
}
