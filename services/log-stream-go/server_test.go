package main

import (
	"bufio"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var testKey = []byte(strings.Repeat("k", 40))

func target() claims {
	now := time.Now().Unix()
	return claims{Version: 1, Namespace: "mlp-project", Pod: "workflow-pod", PodUID: "pod-uid", Workflow: "workflow", WorkflowUID: "workflow-uid", Container: "main", Issued: now, Expires: now + 60, End: now + 300, Nonce: "0123456789abcdef0123456789abcdef"}
}
func sign(c claims) string {
	raw, _ := json.Marshal(c)
	return signPayload(raw)
}
func signPayload(raw []byte) string {
	payload := base64.RawURLEncoding.EncodeToString(raw)
	mac := hmac.New(sha256.New, testKey)
	mac.Write([]byte(payload))
	return payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}
func TestCapabilityValidation(t *testing.T) {
	c := target()
	if got, err := verify(sign(c), testKey, time.Now().Unix()); err != nil || got != c {
		t.Fatalf("valid capability: %v", err)
	}
	cases := map[string]func(*claims){
		"expired":             func(c *claims) { c.Issued -= 61; c.Expires -= 61; c.End -= 61 },
		"future":              func(c *claims) { c.Issued += 30; c.Expires += 30; c.End += 30 },
		"unbounded":           func(c *claims) { c.End++ },
		"long admission":      func(c *claims) { c.Expires++ },
		"namespace traversal": func(c *claims) { c.Namespace = "mlp-../other" },
		"pod traversal":       func(c *claims) { c.Pod = "../pod" },
		"foreign namespace":   func(c *claims) { c.Namespace = "kube-system" },
		"sidecar":             func(c *claims) { c.Container = "wait" },
		"missing UID":         func(c *claims) { c.PodUID = "" },
		"missing owner":       func(c *claims) { c.WorkflowUID = "" },
		"nonce":               func(c *claims) { c.Nonce = "x" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			bad := c
			mutate(&bad)
			if _, err := verify(sign(bad), testKey, time.Now().Unix()); err == nil {
				t.Fatal("accepted invalid claim")
			}
		})
	}
	raw, _ := json.Marshal(c)
	unknown := append(append([]byte{}, raw[:len(raw)-1]...), []byte(`,"extra":true}`)...)
	for _, token := range []string{sign(c) + "x", signPayload(unknown), signPayload(append(raw, raw...)), strings.Repeat("x", 4097)} {
		if _, err := verify(token, testKey, time.Now().Unix()); err == nil {
			t.Fatal("accepted malformed capability")
		}
	}
}

func fixture(t *testing.T, logs http.HandlerFunc, swapUID bool) (*service, *httptest.Server, string) {
	t.Helper()
	var reads atomic.Int32
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("service-account-token"), 0600); err != nil {
		t.Fatal(err)
	}
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer service-account-token" {
			http.Error(w, "auth", 401)
			return
		}
		c := target()
		switch r.URL.Path {
		case podPath(c):
			uid := c.PodUID
			if reads.Add(1) > 1 && swapUID {
				uid = "replacement"
			}
			fmt.Fprintf(w, `{"metadata":{"uid":%q,"labels":{"workflows.argoproj.io/workflow":"workflow"},"ownerReferences":[{"kind":"Workflow","uid":"workflow-uid"}]},"spec":{"containers":[{"name":"main"}]}}`, uid)
		case podPath(c) + "/log":
			if r.URL.Query().Get("container") != "main" || r.URL.Query().Get("follow") != "true" || r.URL.Query().Get("tailLines") != "2000" {
				t.Error("unsafe log query")
			}
			logs(w, r)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(upstream.Close)
	s := &service{key: testKey, kube: &kubeClient{client: upstream.Client(), endpoint: upstream.URL, tokenFile: token}, byNamespace: map[string]int{}, used: map[string]int64{}, heartbeat: 20 * time.Millisecond}
	front := httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(front.Close)
	return s, front, sign(target())
}
func open(t *testing.T, front *httptest.Server, token string) *http.Response {
	t.Helper()
	r, _ := http.NewRequest("GET", front.URL+"/log-stream", nil)
	r.Header.Set("Authorization", "Bearer "+token)
	response, err := front.Client().Do(r)
	if err != nil {
		t.Fatal(err)
	}
	return response
}
func TestStreamEOFAndReplay(t *testing.T) {
	s, front, token := fixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "line one\n<script>safe text</script>\n") }, false)
	response := open(t, front, token)
	defer response.Body.Close()
	raw, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	if response.StatusCode != 200 || !strings.Contains(text, "event: ready") || !strings.Contains(text, "line one\\n") || !strings.Contains(text, `"reason":"complete"`) || strings.Contains(text, "<script>") {
		t.Fatalf("unsafe/incomplete SSE: %s", text)
	}
	replay := open(t, front, token)
	replay.Body.Close()
	if replay.StatusCode != 429 {
		t.Fatal("replayed capability accepted")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.active != 0 {
		t.Fatal("leaked stream slot")
	}
}
func TestPodReplacementAndMissingOwner(t *testing.T) {
	_, front, token := fixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "secret log\n") }, true)
	response := open(t, front, token)
	defer response.Body.Close()
	raw, _ := io.ReadAll(response.Body)
	if response.StatusCode != 403 || strings.Contains(string(raw), "secret log") {
		t.Fatal("replacement pod exposed logs")
	}
}
func TestCancellationClosesUpstream(t *testing.T) {
	cancelled := make(chan struct{})
	s, front, token := fixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(cancelled)
	}, false)
	response := open(t, front, token)
	reader := bufio.NewReader(response.Body)
	line, err := reader.ReadString('\n')
	if err != nil || line != "event: ready\n" {
		t.Fatal("did not stream before EOF")
	}
	response.Body.Close()
	select {
	case <-cancelled:
	case <-time.After(2 * time.Second):
		t.Fatal("upstream not cancelled")
	}
	deadline := time.Now().Add(time.Second)
	for {
		s.mu.Lock()
		active := s.active
		s.mu.Unlock()
		if active == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("slot leaked after cancel")
		}
		time.Sleep(time.Millisecond)
	}
}
func TestOversizedLineIsBounded(t *testing.T) {
	_, front, token := fixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, strings.Repeat("x", 128<<10)) }, false)
	response := open(t, front, token)
	defer response.Body.Close()
	raw, _ := io.ReadAll(response.Body)
	if !strings.Contains(string(raw), "event: error") || len(raw) > 1000 {
		t.Fatal("oversized upstream data not bounded")
	}
}
func TestOriginAndQueryRejection(t *testing.T) {
	_, front, token := fixture(t, func(w http.ResponseWriter, r *http.Request) {}, false)
	for _, test := range []struct {
		path, origin string
		status       int
	}{{"/log-stream?token=" + token, "", 400}, {"/log-stream", "https://evil.example", 403}} {
		r, _ := http.NewRequest("GET", front.URL+test.path, nil)
		r.Header.Set("Origin", test.origin)
		r.Header.Set("Authorization", "Bearer "+token)
		response, err := front.Client().Do(r)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != test.status {
			t.Fatalf("got %d", response.StatusCode)
		}
	}
}
func TestAdmissionLimitsUnderConcurrency(t *testing.T) {
	s := &service{byNamespace: map[string]int{}, used: map[string]int64{}}
	var wg sync.WaitGroup
	var admitted atomic.Int32
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c := target()
			c.Nonce = fmt.Sprintf("%032x", i)
			if s.acquire(c) {
				admitted.Add(1)
			}
		}(i)
	}
	wg.Wait()
	if admitted.Load() != 8 {
		t.Fatalf("namespace bound: %d", admitted.Load())
	}
	s.active = 64
	c := target()
	c.Namespace = "mlp-other"
	c.Nonce = strings.Repeat("f", 32)
	if s.acquire(c) {
		t.Fatal("global bound exceeded")
	}
	s.active = 0
	s.draining.Store(true)
	if s.acquire(c) {
		t.Fatal("admission during shutdown")
	}
}
func TestTokenRotationAndTLS(t *testing.T) {
	s, _, _ := fixture(t, func(w http.ResponseWriter, r *http.Request) {}, false)
	if !s.kube.owns(context.Background(), target()) {
		t.Fatal("expected owner")
	}
	if err := os.WriteFile(s.kube.tokenFile, []byte("rotated-token"), 0600); err != nil {
		t.Fatal(err)
	}
	if s.kube.owns(context.Background(), target()) {
		t.Fatal("cached service-account token")
	}
	s.kube.client = &http.Client{Timeout: time.Second}
	if s.kube.owns(context.Background(), target()) {
		t.Fatal("accepted untrusted TLS certificate")
	}
}
