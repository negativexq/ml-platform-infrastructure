package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type service struct {
	key         []byte
	kube        *kubeClient
	origin      string
	mu          sync.Mutex
	active      int
	byNamespace map[string]int
	used        map[string]int64
	admitted    atomic.Uint64
	denied      atomic.Uint64
	draining    atomic.Bool
	heartbeat   time.Duration
}

func (s *service) acquire(c claims) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().Unix()
	for nonce, expires := range s.used {
		if expires <= now {
			delete(s.used, nonce)
		}
	}
	if s.draining.Load() || s.active >= 64 || s.byNamespace[c.Namespace] >= 8 || len(s.used) >= 8192 || s.used[c.Nonce] != 0 {
		return false
	}
	s.used[c.Nonce] = c.Expires
	s.active++
	s.byNamespace[c.Namespace]++
	s.admitted.Add(1)
	return true
}
func (s *service) release(c claims) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.active--
	s.byNamespace[c.Namespace]--
	if s.byNamespace[c.Namespace] == 0 {
		delete(s.byNamespace, c.Namespace)
	}
}
func event(w http.ResponseWriter, kind string, data any) error {
	controller := http.NewResponseController(w)
	if err := controller.SetWriteDeadline(time.Now().Add(10 * time.Second)); err != nil && !errors.Is(err, http.ErrNotSupported) {
		return err
	}
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", kind, payload); err != nil {
		return err
	}
	return controller.Flush()
}

func (s *service) serve(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	origin := r.Header.Get("Origin")
	if origin != "" {
		u, err := url.Parse(origin)
		allowed := err == nil && u.Host == r.Host && (u.Scheme == "https" || u.Scheme == "http") && u.Path == "" && u.RawQuery == "" && u.Fragment == "" && u.User == nil
		if s.origin != "" {
			allowed = origin == s.origin
		}
		if !allowed {
			http.Error(w, "origin denied", 403)
			return
		}
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Vary", "Origin")
	}
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "GET")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization")
		w.WriteHeader(204)
		return
	}
	if r.Method != http.MethodGet || r.URL.RawQuery != "" {
		http.Error(w, "invalid request", 400)
		return
	}
	if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") {
		s.denied.Add(1)
		http.Error(w, "capability required", 401)
		return
	}
	c, err := verify(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), s.key, time.Now().Unix())
	if err != nil {
		s.denied.Add(1)
		http.Error(w, "invalid capability", 401)
		return
	}
	if !s.acquire(c) {
		s.denied.Add(1)
		w.Header().Set("Retry-After", "3")
		http.Error(w, "stream limit or used capability", 429)
		return
	}
	defer s.release(c)
	ctx, cancel := context.WithDeadline(r.Context(), time.Unix(c.End, 0))
	defer cancel()
	if !s.kube.owns(ctx, c) {
		http.Error(w, "target unavailable", 403)
		return
	}
	response, err := s.kube.get(ctx, podPath(c)+"/log?container=main&follow=true&timestamps=true&tailLines=2000")
	if err != nil {
		http.Error(w, "logs unavailable", 503)
		return
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		http.Error(w, "logs unavailable", 503)
		return
	}
	// Recheck after opening logs to reject a same-name pod replacement before emitting data.
	if !s.kube.owns(ctx, c) {
		http.Error(w, "target changed", 403)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("X-Accel-Buffering", "no")
	if event(w, "ready", map[string]string{"pod": c.Pod}) != nil {
		return
	}
	type result struct {
		line   string
		done   bool
		failed bool
	}
	lines := make(chan result, 4)
	go func() {
		scanner := bufio.NewScanner(response.Body)
		scanner.Buffer(make([]byte, 4096), 64<<10)
		for scanner.Scan() {
			select {
			case lines <- result{line: scanner.Text()}:
			case <-ctx.Done():
				return
			}
		}
		select {
		case lines <- result{done: true, failed: scanner.Err() != nil}:
		case <-ctx.Done():
		}
	}()
	ticker := time.NewTicker(s.heartbeat)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			if r.Context().Err() == nil {
				_ = event(w, "end", map[string]string{"reason": "timeout"})
			}
			return
		case <-ticker.C:
			if event(w, "heartbeat", nil) != nil {
				return
			}
		case item := <-lines:
			if item.done {
				if item.failed {
					_ = event(w, "error", map[string]string{"reason": "upstream"})
				} else {
					_ = event(w, "end", map[string]string{"reason": "complete"})
				}
				return
			}
			if event(w, "log", item.line+"\n") != nil {
				return
			}
		}
	}
}
