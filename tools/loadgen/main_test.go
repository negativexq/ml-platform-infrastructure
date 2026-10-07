package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"syscall"
	"testing"
	"time"
)

func fixture(targets ...string) config {
	return config{RPS: 50, Duration: 200 * time.Millisecond, Timeout: time.Second, Drain: time.Second, Concurrency: 4, Queue: 32, Targets: targets, Payload: []byte(`{}`)}
}
func TestBalancedTargetsAndStatuses(t *testing.T) {
	handler := func(status int) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Method != "POST" || r.Header.Get("Authorization") != "Bearer fixture" {
				t.Error("request contract")
			}
			io.Copy(io.Discard, r.Body)
			w.WriteHeader(status)
		}
	}
	a := httptest.NewServer(handler(200))
	defer a.Close()
	b := httptest.NewServer(handler(429))
	defer b.Close()
	c := fixture(a.URL, b.URL)
	c.Token = "fixture"
	r := run(context.Background(), c)
	if r.Offered != 10 || r.Completed != 10 || r.Errors != 0 || r.QueueDropped != 0 || r.Status[200] != 5 || r.Status[429] != 5 {
		t.Fatalf("unexpected report: %+v", r)
	}
	if r.Elapsed < c.Duration.Seconds() {
		t.Fatal("shortened measurement window")
	}
	for _, target := range r.Targets {
		if target.Completed != 5 {
			t.Fatal("unbalanced target")
		}
	}
}
func TestOverloadIsVisible(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { time.Sleep(80 * time.Millisecond); w.WriteHeader(200) }))
	defer server.Close()
	c := fixture(server.URL)
	c.RPS = 200
	c.Concurrency = 1
	c.Queue = 1
	r := run(context.Background(), c)
	if r.QueueDropped == 0 || r.Queue.P95 < 20 {
		t.Fatalf("overload hidden: %+v", r)
	}
	if r.Offered != r.Started+r.QueueDropped+r.Canceled {
		t.Fatal("offered requests disappeared")
	}
}
func TestStreamingAndTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("first"))
		w.(http.Flusher).Flush()
		time.Sleep(80 * time.Millisecond)
		w.Write([]byte("last"))
	}))
	defer server.Close()
	c := fixture(server.URL)
	c.RPS = 10
	c.Duration = 100 * time.Millisecond
	c.Stream = true
	r := run(context.Background(), c)
	if r.FirstByte.P95 >= r.Latency.P95 || r.Latency.P95 < 70 || r.Errors != 0 {
		t.Fatalf("stream timings: %+v", r)
	}
	c.Timeout = 20 * time.Millisecond
	r = run(context.Background(), c)
	if r.Errors != 1 || r.Completed != 0 || r.Status[200] != 1 {
		t.Fatalf("partial stream error hidden: %+v", r)
	}
}

func TestFailureClassificationDoesNotExposeErrorText(t *testing.T) {
	for _, tc := range []struct {
		err  error
		kind string
	}{
		{fmt.Errorf("private target: %w", context.DeadlineExceeded), "deadline"},
		{fmt.Errorf("private target: %w", io.EOF), "eof"},
		{fmt.Errorf("private target: %w", syscall.ECONNRESET), "connection_reset"},
		{errors.New("sensitive target, token, payload"), "other"},
	} {
		if got := errorKind(tc.err); got != tc.kind {
			t.Fatalf("classification %s != %s", got, tc.kind)
		}
	}
}

func TestConnectionFailureIsReported(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	target := server.URL
	server.Close()
	c := fixture(target)
	c.Duration = 100 * time.Millisecond
	c.RPS = 10
	r := run(context.Background(), c)
	if r.Errors != 1 || r.ErrorKinds["connection_refused"] != 1 || len(r.ErrorSamples) != 1 {
		t.Fatalf("failure detail missing: %+v", r)
	}
	if r.ErrorSamples[0].Phase != "headers" || r.ErrorSamples[0].ElapsedMS <= 0 {
		t.Fatal("invalid timing/phase")
	}
}

func TestConnectionModes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.Copy(io.Discard, r.Body); w.WriteHeader(429) }))
	defer server.Close()
	for _, mode := range []string{"baseline", "short-idle", "no-keepalive"} {
		t.Run(mode, func(t *testing.T) {
			c := fixture(server.URL)
			c.Concurrency = 1
			c.ConnectionDiagnostics = true
			if mode == "short-idle" {
				c.IdleConnTimeout = time.Millisecond
			}
			c.DisableKeepAlives = mode == "no-keepalive"
			r := run(context.Background(), c)
			if r.Completed != 10 || r.Errors != 0 || r.NewConnections+r.ReusedConnections != 10 {
				t.Fatalf("connection accounting: %+v", r)
			}
			if mode == "baseline" && r.ReusedConnections == 0 {
				t.Fatal("baseline did not reuse connections")
			}
			if mode != "baseline" && (r.ReusedConnections != 0 || r.NewConnections != 10) {
				t.Fatalf("connections not expired/disabled: %+v", r)
			}
			if r.KeepAliveDisabled != c.DisableKeepAlives {
				t.Fatal("wrong transport report")
			}
		})
	}
}
