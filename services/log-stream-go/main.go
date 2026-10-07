package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func main() {
	key, err := os.ReadFile(os.Getenv("LOG_STREAM_SIGNING_KEY_FILE"))
	if err != nil || len(key) < 32 {
		log.Fatal("log signing key file (minimum 32 bytes) required")
	}
	kube, err := configuredKube()
	if err != nil {
		log.Fatal("Kubernetes configuration unavailable")
	}
	origin := strings.TrimSuffix(os.Getenv("LOG_STREAM_ALLOWED_ORIGIN"), "/")
	if origin != "" {
		u, err := url.Parse(origin)
		if err != nil || u.Host == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "https" && !(u.Scheme == "http" && (u.Hostname() == "127.0.0.1" || u.Hostname() == "localhost" || u.Hostname() == "::1"))) {
			log.Fatal("invalid allowed origin")
		}
	}
	s := &service{key: key, kube: kube, origin: origin, byNamespace: make(map[string]int), used: make(map[string]int64), heartbeat: 15 * time.Second}
	mux := http.NewServeMux()
	mux.HandleFunc("/log-stream", s.serve)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		if s.draining.Load() {
			w.WriteHeader(503)
			return
		}
		w.WriteHeader(200)
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		active := s.active
		s.mu.Unlock()
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprintf(w, "mlp_log_stream_active %d\nmlp_log_stream_admitted_total %d\nmlp_log_stream_denied_total %d\n", active, s.admitted.Load(), s.denied.Load())
	})
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	server := &http.Server{Addr: ":8082", Handler: mux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10, BaseContext: func(net.Listener) context.Context { return ctx }}
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		<-ctx.Done()
		s.draining.Store(true)
		deadline, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = server.Shutdown(deadline)
	}()
	log.Print("Go log streaming listening on :8082")
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal("log server failed")
	}
	<-stopped
}
