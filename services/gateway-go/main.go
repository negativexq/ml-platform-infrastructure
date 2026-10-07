package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func cgroupRuntime(workers, capacity int, idle time.Duration) map[string]any {
	readInt := func(name string) int64 {
		b, _ := os.ReadFile(filepath.Join("/sys/fs/cgroup", name))
		v, _ := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
		return v
	}
	cpu := map[string]int64{}
	b, _ := os.ReadFile("/sys/fs/cgroup/cpu.stat")
	for _, line := range strings.Split(string(b), "\n") {
		pair := strings.Fields(line)
		if len(pair) == 2 {
			v, _ := strconv.ParseInt(pair[1], 10, 64)
			cpu[pair[0]] = v
		}
	}
	return map[string]any{"at_epoch": float64(time.Now().UnixNano()) / 1e9, "cpu": cpu, "memory_bytes": readInt("memory.current"), "memory_peak_bytes": readInt("memory.peak"), "goroutines": runtime.NumGoroutine(), "runtime_kind": "go", "server_keep_alive_seconds": idle.Seconds(), "workers": workers, "pool_capacity": capacity, "telemetry_profile": normalProfile(), "otel_sdk_disabled": os.Getenv("OTEL_SDK_DISABLED") == "true"}
}
func runtimeHandler(t *telemetry, workers, capacity int, idle time.Duration) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(cgroupRuntime(workers, capacity, idle))
	})
	mux.HandleFunc("POST /telemetry/flush", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		if t.flush(ctx) != nil {
			w.WriteHeader(503)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]bool{"flushed": true})
	})
	return mux
}
func capacitySetting(primary, legacy string, fallback int) int {
	raw := os.Getenv(primary)
	if raw == "" {
		raw = os.Getenv(legacy)
	}
	if raw == "" {
		return fallback
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 || n > 256 {
		log.Fatal("invalid capacity configuration")
	}
	return n
}
func idleTimeoutSetting() (time.Duration, error) {
	raw := os.Getenv("CP_GATEWAY_IDLE_TIMEOUT")
	if raw == "" {
		return 60 * time.Second, nil
	}
	d, err := time.ParseDuration(raw)
	if err != nil || d < time.Second || d > 10*time.Minute {
		return 0, errors.New("gateway idle timeout must be between 1s and 10m")
	}
	return d, nil
}
func publicServer(handler http.Handler, idle time.Duration) *http.Server {
	return &http.Server{Addr: ":8081", Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second, IdleTimeout: idle, MaxHeaderBytes: 1 << 20}
}
func main() {
	if len(os.Args) == 2 && strings.HasPrefix(os.Args[1], "--drain-wait=") {
		d, e := time.ParseDuration(strings.TrimPrefix(os.Args[1], "--drain-wait="))
		if e != nil || d < 0 || d > 30*time.Second {
			log.Fatal("invalid drain duration")
		}
		time.Sleep(d)
		return
	}

	if profile := os.Getenv("CP_GATEWAY_OBSERVABILITY_PROFILE"); profile != "" && profile != "normal" {
		log.Fatal("Go gateway currently supports the normal observability profile")
	}
	idle, err := idleTimeoutSetting()
	if err != nil {
		log.Fatal("invalid gateway idle timeout")
	}
	workers := capacitySetting("CP_GATEWAY_WORKERS", "PROBE_WORKERS", 12)
	capacity := capacitySetting("CP_GATEWAY_DB_POOL_CAPACITY", "PROBE_POOL_CAPACITY", 15)
	if os.Getenv("CP_DATABASE_URL") == "" {
		log.Fatal("database configuration missing")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	t, err := setupTelemetry(ctx)
	if err != nil {
		log.Fatal("telemetry initialization failed")
	}
	s, err := openStore(ctx, os.Getenv("CP_DATABASE_URL"), capacity, t)
	if err != nil {
		log.Fatal("database initialization failed")
	}
	defer s.pool.Close()
	ready, done := context.WithTimeout(ctx, 5*time.Second)
	err = s.Ready(ready)
	done()
	if err != nil {
		log.Fatal("database readiness failed")
	}
	g := newGateway(s, workers, t)
	mode := os.Getenv("CP_AUTH_MODE")
	if mode == "" {
		mode = "oidc"
	}
	switch mode {
	case "none":
	case "oidc":
		audience := os.Getenv("CP_OIDC_AUDIENCE")
		if audience == "" {
			audience = "mlp"
		}
		auth, e := newOIDC(os.Getenv("CP_OIDC_ISSUER"), audience, os.Getenv("CP_OIDC_USERNAME_CLAIM"), os.Getenv("CP_OIDC_GROUPS_CLAIM"), strings.Split(os.Getenv("CP_PLATFORM_ADMINS"), ","))
		if e != nil {
			log.Fatal("invalid identity configuration")
		}
		g.identity = auth
	default:
		log.Fatal("invalid authentication mode")
	}

	public := publicServer(g, idle)
	// Probe diagnostics bind the same private pod-only port as the Python fixture.
	probe := &http.Server{Addr: ":8082", Handler: runtimeHandler(t, workers, capacity, idle), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second}
	if os.Getenv("CP_GATEWAY_PROBE_ENABLED") == "true" {
		go func() {
			if err := probe.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Print("probe server stopped")
				stop()
			}
		}()
	}
	go func() {
		if err := public.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Print("gateway server stopped")
			stop()
		}
	}()
	log.Print("Go gateway ready")
	<-ctx.Done()
	shutdown, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	_ = public.Shutdown(shutdown)
	_ = probe.Shutdown(shutdown)
	t.shutdown(shutdown)
	g.client.CloseIdleConnections()
}
