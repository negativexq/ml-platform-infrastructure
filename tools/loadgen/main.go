// mlp-loadgen sends open-loop HTTP load with bounded, explicitly reported queueing.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"os"
	"os/signal"
	"runtime"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/time/rate"
)

type config struct {
	RPS                                      float64
	Duration, Timeout, Drain                 time.Duration
	Concurrency, Queue                       int
	Targets                                  []string
	Payload                                  []byte
	Stream                                   bool
	IdleConnTimeout                          time.Duration
	DisableKeepAlives, ConnectionDiagnostics bool
	Token                                    string
}
type quantiles struct {
	Count int     `json:"count"`
	P50   float64 `json:"p50"`
	P95   float64 `json:"p95"`
	P99   float64 `json:"p99"`
}

func percentile(values []float64) quantiles {
	if len(values) == 0 {
		return quantiles{}
	}
	sort.Float64s(values)
	at := func(p float64) float64 { return values[int(math.Ceil(p*float64(len(values))))-1] }
	return quantiles{Count: len(values), P50: at(.5), P95: at(.95), P99: at(.99)}
}

type targetResult struct {
	Started   int     `json:"started"`
	Completed int     `json:"completed"`
	Errors    int     `json:"transport_errors"`
	RPS       float64 `json:"completed_rps"`
}
type transportFailure struct {
	Kind              string  `json:"kind"`
	Phase             string  `json:"phase"`
	AtSeconds         float64 `json:"at_seconds"`
	ElapsedMS         float64 `json:"elapsed_ms"`
	ConnectionReused  bool    `json:"connection_reused"`
	ConnectionWasIdle bool    `json:"connection_was_idle"`
	ConnectionIdleMS  float64 `json:"connection_idle_ms"`
	GotConnection     bool    `json:"got_connection"`
}

func errorKind(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return "deadline"
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	var timed net.Error
	if errors.As(err, &timed) && timed.Timeout() {
		return "timeout"
	}
	if errors.Is(err, syscall.ECONNRESET) {
		return "connection_reset"
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		return "connection_refused"
	}
	if errors.Is(err, syscall.EPIPE) {
		return "broken_pipe"
	}
	if errors.Is(err, io.EOF) {
		return "eof"
	}
	if errors.Is(err, io.ErrUnexpectedEOF) {
		return "unexpected_eof"
	}
	var operation *net.OpError
	if errors.As(err, &operation) {
		return "network"
	}
	return "other"
}

type report struct {
	IdleConnTimeoutSeconds float64                  `json:"idle_conn_timeout_seconds"`
	KeepAliveDisabled      bool                     `json:"keep_alive_disabled"`
	ConnectionDiagnostics  bool                     `json:"connection_diagnostics"`
	NewConnections         int                      `json:"new_connections"`
	ReusedConnections      int                      `json:"reused_connections"`
	Planned                int                      `json:"planned"`
	Offered                int                      `json:"offered"`
	Started                int                      `json:"started"`
	Completed              int                      `json:"completed"`
	QueueDropped           int                      `json:"queue_dropped"`
	Unscheduled            int                      `json:"unscheduled"`
	Canceled               int                      `json:"canceled_before_start"`
	Errors                 int                      `json:"transport_errors"`
	ErrorKinds             map[string]int           `json:"transport_error_kinds"`
	ErrorSamples           []transportFailure       `json:"transport_error_samples"`
	OfferedRPS             float64                  `json:"offered_rps"`
	CompletedRPS           float64                  `json:"completed_rps"`
	WindowCompletedRPS     float64                  `json:"completed_within_window_rps"`
	Elapsed                float64                  `json:"elapsed_seconds"`
	Latency                quantiles                `json:"request_latency_ms"`
	Queue                  quantiles                `json:"queue_delay_ms"`
	Schedule               quantiles                `json:"schedule_lag_ms"`
	FirstByte              quantiles                `json:"first_body_byte_ms"`
	Status                 map[int]int              `json:"status_codes"`
	Targets                map[string]*targetResult `json:"per_target"`
	CPUSeconds             float64                  `json:"process_cpu_seconds"`
	CPUCores               float64                  `json:"process_cpu_cores"`
	PeakRSS                int64                    `json:"peak_rss_bytes"`
	GOMAXPROCS             int                      `json:"gomaxprocs"`
	Stream                 bool                     `json:"stream"`
	InWindow               int                      `json:"completed_within_window"`
}
type job struct {
	target            int
	planned, enqueued time.Time
}

func run(ctx context.Context, c config) report {
	r := report{Planned: int(math.Ceil(c.RPS * c.Duration.Seconds())), Status: map[int]int{}, ErrorKinds: map[string]int{}, Targets: map[string]*targetResult{}, Stream: c.Stream, GOMAXPROCS: runtime.GOMAXPROCS(0)}
	for i := range c.Targets {
		r.Targets[fmt.Sprintf("target_%d", i)] = &targetResult{}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxConnsPerHost = c.Concurrency
	transport.MaxIdleConns = c.Concurrency * len(c.Targets)
	transport.MaxIdleConnsPerHost = c.Concurrency
	if c.IdleConnTimeout > 0 {
		transport.IdleConnTimeout = c.IdleConnTimeout
	}
	transport.DisableKeepAlives = c.DisableKeepAlives
	r.IdleConnTimeoutSeconds = transport.IdleConnTimeout.Seconds()
	r.KeepAliveDisabled = c.DisableKeepAlives
	r.ConnectionDiagnostics = c.ConnectionDiagnostics
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: c.Timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	jobs := make(chan job, c.Queue)
	var mu sync.Mutex
	var workers sync.WaitGroup
	var latency, queues, lag, firstBytes []float64
	cpuBefore, _ := processUsage()
	start := time.Now()
	end := start.Add(c.Duration)
	ctx, cancel := context.WithTimeout(ctx, c.Duration+c.Drain)
	defer cancel()
	for range c.Concurrency {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for j := range jobs {
				if ctx.Err() != nil {
					mu.Lock()
					r.Canceled++
					mu.Unlock()
					continue
				}
				requestStart := time.Now()
				queue := requestStart.Sub(j.enqueued).Seconds() * 1000
				req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.Targets[j.target], bytes.NewReader(c.Payload))
				if err != nil {
					panic("validated URL failed request construction")
				}
				req.Header.Set("Content-Type", "application/json")
				if c.Token != "" {
					req.Header.Set("Authorization", "Bearer "+c.Token)
				}
				var connectionMu sync.Mutex
				var connection httptrace.GotConnInfo
				gotConnection := false
				if c.ConnectionDiagnostics {
					req = req.WithContext(httptrace.WithClientTrace(req.Context(), &httptrace.ClientTrace{
						GotConn: func(info httptrace.GotConnInfo) {
							connectionMu.Lock()
							connection = info
							gotConnection = true
							connectionMu.Unlock()
							mu.Lock()
							if info.Reused {
								r.ReusedConnections++
							} else {
								r.NewConnections++
							}
							mu.Unlock()
						},
					}))
				}
				status := 0
				first := float64(-1)
				phase := "headers"
				response, err := client.Do(req)
				if err == nil {
					phase = "body"
				}
				if response != nil {
					status = response.StatusCode
					if c.Stream {
						b := make([]byte, 32*1024)
						for {
							n, e := response.Body.Read(b)
							if n > 0 && first < 0 {
								first = time.Since(requestStart).Seconds() * 1000
							}
							if e == io.EOF {
								break
							}
							if e != nil {
								err = e
								break
							}
						}
					} else {
						_, err = io.Copy(io.Discard, response.Body)
					}
					response.Body.Close()
				}
				finished := time.Now()
				connectionMu.Lock()
				connectionInfo, hadConnection := connection, gotConnection
				connectionMu.Unlock()
				mu.Lock()
				tr := r.Targets[fmt.Sprintf("target_%d", j.target)]
				tr.Started++
				r.Started++
				queues = append(queues, queue)
				latency = append(latency, finished.Sub(requestStart).Seconds()*1000)
				if status != 0 {
					r.Status[status]++
				}
				if first >= 0 {
					firstBytes = append(firstBytes, first)
				}
				if err != nil {
					r.Errors++
					kind := errorKind(err)
					r.ErrorKinds[kind]++
					if len(r.ErrorSamples) < 16 {
						r.ErrorSamples = append(r.ErrorSamples, transportFailure{
							Kind: kind, Phase: phase, AtSeconds: finished.Sub(start).Seconds(),
							ElapsedMS:        finished.Sub(requestStart).Seconds() * 1000,
							ConnectionReused: connectionInfo.Reused, ConnectionWasIdle: connectionInfo.WasIdle,
							ConnectionIdleMS: connectionInfo.IdleTime.Seconds() * 1000, GotConnection: hadConnection,
						})
					}
					tr.Errors++
				} else {
					r.Completed++
					tr.Completed++
					if finished.Before(end) {
						r.InWindow++
					}
				}
				mu.Unlock()
			}
		}()
	}
	scheduleCtx, stop := context.WithDeadline(ctx, end)
	limiter := rate.NewLimiter(rate.Limit(c.RPS), 1)
	for i := 0; i < r.Planned; i++ {
		// One epoch preserves planned slots after a late scheduler wakeup.
		reservation := limiter.ReserveN(start, 1)
		planned := start.Add(reservation.DelayFrom(start))
		if delay := time.Until(planned); delay > 0 {
			timer := time.NewTimer(delay)
			select {
			case <-timer.C:
			case <-scheduleCtx.Done():
				timer.Stop()
			}
			timer.Stop()
		}
		if scheduleCtx.Err() != nil {
			break
		}
		now := time.Now()
		j := job{i % len(c.Targets), planned, now}
		r.Offered++
		lag = append(lag, math.Max(0, now.Sub(j.planned).Seconds()*1000))
		select {
		case jobs <- j:
		default:
			r.QueueDropped++
		}
	}
	stop()
	close(jobs)
	workers.Wait()
	if remaining := time.Until(end); remaining > 0 {
		timer := time.NewTimer(remaining)
		select {
		case <-timer.C:
		case <-ctx.Done():
		}
		timer.Stop()
	}
	r.Elapsed = time.Since(start).Seconds()
	r.Unscheduled = r.Planned - r.Offered
	r.OfferedRPS = float64(r.Offered) / c.Duration.Seconds()
	r.CompletedRPS = float64(r.Completed) / r.Elapsed
	r.WindowCompletedRPS = float64(r.InWindow) / c.Duration.Seconds()
	r.Latency = percentile(latency)
	r.Queue = percentile(queues)
	r.Schedule = percentile(lag)
	r.FirstByte = percentile(firstBytes)
	cpuAfter, rss := processUsage()
	r.CPUSeconds = math.Max(0, cpuAfter-cpuBefore)
	r.CPUCores = r.CPUSeconds / r.Elapsed
	r.PeakRSS = rss
	for _, tr := range r.Targets {
		tr.RPS = float64(tr.Completed) / r.Elapsed
	}
	return r
}
func main() {
	var c config
	var targets, payload string
	flag.Float64Var(&c.RPS, "rps", 100, "offered requests/sec")
	flag.DurationVar(&c.Duration, "duration", 10*time.Second, "offering window")
	flag.DurationVar(&c.Timeout, "timeout", 10*time.Second, "per-request timeout, including body")
	flag.DurationVar(&c.Drain, "drain-timeout", 30*time.Second, "maximum drain after offering")
	flag.IntVar(&c.Concurrency, "concurrency", 200, "HTTP workers and per-host connection cap")
	flag.IntVar(&c.Queue, "queue-size", 400, "bounded pending queue; overflow is explicitly counted")
	flag.StringVar(&targets, "targets", "", "comma-separated full request URLs, round-robin")
	flag.StringVar(&payload, "payload", "{}", "JSON body or @file")
	flag.BoolVar(&c.Stream, "stream", false, "observe first body byte and drain full stream")
	flag.DurationVar(&c.IdleConnTimeout, "idle-conn-timeout", 0, "override idle connection timeout; zero preserves Go default")
	flag.BoolVar(&c.DisableKeepAlives, "disable-keep-alives", false, "open a fresh connection per request")
	flag.BoolVar(&c.ConnectionDiagnostics, "connection-diagnostics", false, "count acquired new/reused connections and include safe error connection state")
	flag.Parse()
	fail := func(message string) { fmt.Fprintln(os.Stderr, message); os.Exit(2) }
	if c.IdleConnTimeout < 0 {
		fail("idle connection timeout must be nonnegative")
	}
	if math.IsNaN(c.RPS) || math.IsInf(c.RPS, 0) || c.RPS <= 0 || c.Duration <= 0 || c.Timeout <= 0 || c.Drain <= 0 || c.Concurrency < 1 || c.Concurrency > 10000 || c.Queue < 0 || c.Queue > 1000000 || c.RPS*c.Duration.Seconds() > 1000000 {
		fail("invalid load bounds (maximum 1,000,000 planned requests)")
	}
	for _, target := range strings.Split(targets, ",") {
		u, e := url.Parse(strings.TrimSpace(target))
		if e != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.Fragment != "" {
			fail("targets must be HTTP(S) URLs without userinfo/fragments")
		}
		c.Targets = append(c.Targets, u.String())
	}
	if len(c.Targets) > 64 {
		fail("maximum 64 targets")
	}
	c.Payload = []byte(payload)
	if strings.HasPrefix(payload, "@") {
		b, e := os.ReadFile(payload[1:])
		if e != nil {
			fail("cannot read payload file")
		}
		c.Payload = b
	}
	if !json.Valid(c.Payload) {
		fail("payload must be JSON")
	}
	c.Token = os.Getenv("MLP_LOADGEN_TOKEN")
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	result := run(ctx, c)
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		fail("cannot write report")
	}
	if result.Errors+result.QueueDropped+result.Unscheduled+result.Canceled > 0 {
		os.Exit(1)
	}
}
