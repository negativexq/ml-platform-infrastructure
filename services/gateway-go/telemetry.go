package main

import (
	"context"
	"math/rand/v2"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

type telemetry struct {
	requests, usage, units, tokenCount, dbErrors                                     metric.Int64Counter
	inflight, workers                                                                metric.Int64UpDownCounter
	phaseDuration, totalDuration, limiterDuration, dbAcquire, dbQuery, dbTransaction metric.Float64Histogram
	meter                                                                            metric.Meter
	provider                                                                         *sdkmetric.MeterProvider
	traces                                                                           *sdktrace.TracerProvider
}

func instruments(m metric.Meter) *telemetry {
	t := &telemetry{meter: m}
	t.requests, _ = m.Int64Counter("mlp.gateway.requests", metric.WithUnit("{request}"))
	t.usage, _ = m.Int64Counter("mlp.gateway.usage.requests", metric.WithUnit("{request}"))
	t.tokenCount, _ = m.Int64Counter("mlp.gateway.tokens", metric.WithUnit("{token}"))
	t.units, _ = m.Int64Counter("mlp.gateway.units", metric.WithUnit("{unit}"))
	t.dbErrors, _ = m.Int64Counter("mlp.db.errors")
	t.inflight, _ = m.Int64UpDownCounter("mlp.gateway.inflight")
	t.workers, _ = m.Int64UpDownCounter("mlp.gateway.limiter.workers")
	histogram := func(name string, buckets []float64) metric.Float64Histogram {
		h, _ := m.Float64Histogram(name, metric.WithUnit("s"), metric.WithExplicitBucketBoundaries(buckets...))
		return h
	}
	phase := []float64{.001, .005, .01, .025, .05, .1, .25, .5, 1, 2, 5, 30}
	db := []float64{.001, .005, .01, .025, .05, .1, .25, .5, 1, 2, 3, 6}
	t.phaseDuration = histogram("mlp.gateway.phase.duration", phase)
	t.totalDuration = histogram("mlp.gateway.duration", []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30})
	t.limiterDuration = histogram("mlp.gateway.limiter.duration", db)
	t.dbAcquire = histogram("mlp.db.pool.acquire.duration", db)
	t.dbQuery = histogram("mlp.db.query.duration", db)
	t.dbTransaction = histogram("mlp.db.transaction.duration", db)
	return t
}
func setupTelemetry(ctx context.Context) (*telemetry, error) {
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	if os.Getenv("OTEL_SDK_DISABLED") == "true" {
		return instruments(noop.NewMeterProvider().Meter("mlp.gateway.go")), nil
	}
	attrs := []attribute.KeyValue{attribute.String("service.name", "mlp-gateway")}
	for key, env := range map[string]string{"service.instance.id": "MLP_POD_UID", "k8s.pod.uid": "MLP_POD_UID", "k8s.pod.name": "MLP_POD_NAME", "k8s.namespace.name": "MLP_POD_NAMESPACE", "k8s.node.name": "MLP_NODE_NAME"} {
		if value := os.Getenv(env); value != "" {
			attrs = append(attrs, attribute.String(key, value))
		}
	}
	res, err := resource.New(ctx, resource.WithAttributes(attrs...), resource.WithFromEnv())
	if err != nil {
		return nil, err
	}
	t := instruments(noop.NewMeterProvider().Meter("mlp.gateway.go"))
	if os.Getenv("OTEL_METRICS_EXPORTER") != "none" && (os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || os.Getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") != "") {
		exporter, err := otlpmetrichttp.New(ctx)
		if err != nil {
			return nil, err
		}
		provider := sdkmetric.NewMeterProvider(sdkmetric.WithResource(res), sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter, sdkmetric.WithInterval(30*time.Second))))
		t = instruments(provider.Meter("mlp.gateway.go"))
		t.provider = provider
		otel.SetMeterProvider(provider)
	}
	if os.Getenv("OTEL_TRACES_EXPORTER") != "none" && (os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") != "") {
		exporter, err := otlptracehttp.New(ctx)
		if err != nil {
			t.shutdown(ctx)
			return nil, err
		}
		tp := sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(.01))), sdktrace.WithBatcher(exporter))
		t.traces = tp
		otel.SetTracerProvider(tp)
	}
	return t, nil
}
func (t *telemetry) record(ctx context.Context, k *apiKey, r *route, status, units int, d time.Duration) {
	project, endpoint, caller := "(unknown)", "(unknown)", "(anonymous)"
	if r != nil {
		project = r.Project
		endpoint = r.Endpoint
	}
	if k != nil {
		caller = k.Name
	}
	attrs := []attribute.KeyValue{attribute.String("project", project), attribute.String("endpoint", endpoint), attribute.String("code", strconv.Itoa(status))}
	t.requests.Add(ctx, 1, metric.WithAttributes(attrs...))
	if status >= 400 {
		t.usage.Add(ctx, 1, metric.WithAttributes(append(attrs, attribute.String("caller", caller))...))
	}
	unit := "requests"
	if r != nil && (r.Kind == "llm" || r.Protocol == "openai") {
		unit = "tokens"
	}
	if units > 0 {
		t.units.Add(ctx, int64(units), metric.WithAttributes(attribute.String("project", project), attribute.String("endpoint", endpoint), attribute.String("caller", caller), attribute.String("unit", unit)))
	}
	if rand.Float64() < .2 {
		t.totalDuration.Record(ctx, d.Seconds(), metric.WithAttributes(attribute.String("project", project), attribute.String("endpoint", endpoint)))
	}
}
func (t *telemetry) phase(ctx context.Context, name string, start time.Time, err error) {
	if rand.Float64() >= .2 {
		return
	}
	outcome := "ok"
	if err != nil {
		outcome = "error"
	}
	t.phaseDuration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(attribute.String("phase", name), attribute.String("outcome", outcome)))
}
func (t *telemetry) database(ctx context.Context, op string, start time.Time, err error) {
	outcome := "ok"
	if err != nil {
		outcome = "error"
		t.dbErrors.Add(ctx, 1, metric.WithAttributes(attribute.String("db.system", "postgresql"), attribute.String("operation", op)))
	}
	if rand.Float64() >= .2 {
		return
	}
	h := t.dbQuery
	if op == "acquire" {
		h = t.dbAcquire
	} else if op == "transaction" {
		h = t.dbTransaction
	}
	h.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(attribute.String("db.system", "postgresql"), attribute.String("outcome", outcome)))
}
func (t *telemetry) limiter(ctx context.Context, start time.Time, a allowance, err error) {
	if rand.Float64() >= .2 {
		return
	}
	outcome := "denied"
	if err != nil {
		outcome = "error"
	} else if a.Allowed {
		outcome = "allowed"
	}
	t.limiterDuration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(attribute.String("operation", "take"), attribute.String("outcome", outcome)))
}
func (t *telemetry) observePool(pool *pgxpool.Pool) {
	checked, _ := t.meter.Int64ObservableGauge("mlp.db.pool.checked_out")
	idle, _ := t.meter.Int64ObservableGauge("mlp.db.pool.idle")
	capacity, _ := t.meter.Int64ObservableGauge("mlp.db.pool.capacity")
	_, _ = t.meter.RegisterCallback(func(ctx context.Context, o metric.Observer) error {
		s := pool.Stat()
		opt := metric.WithAttributes(attribute.String("db.system", "postgresql"))
		o.ObserveInt64(checked, int64(s.AcquiredConns()), opt)
		o.ObserveInt64(idle, int64(s.IdleConns()), opt)
		o.ObserveInt64(capacity, int64(s.MaxConns()), opt)
		return nil
	}, checked, idle, capacity)
}
func statusAttribute(status int) attribute.KeyValue {
	return attribute.Int("http.response.status_code", status)
}
func (t *telemetry) flush(ctx context.Context) error {
	if t.provider != nil {
		if err := t.provider.ForceFlush(ctx); err != nil {
			return err
		}
	}
	if t.traces != nil {
		return t.traces.ForceFlush(ctx)
	}
	return nil
}
func (t *telemetry) shutdown(ctx context.Context) {
	if t.provider != nil {
		_ = t.provider.Shutdown(ctx)
	}
	if t.traces != nil {
		_ = t.traces.Shutdown(ctx)
	}
}
func normalProfile() map[string]any {
	return map[string]any{"name": "normal", "sql_tracing": false, "latency_sample_rate": .2, "trace_sample_rate": .01, "request_caller_label": false, "native_http_metrics": false, "operation_spans": false, "export_interval_ms": 30000}
}
func requireNormalProfile() bool {
	return os.Getenv("CP_GATEWAY_OBSERVABILITY_PROFILE") == "normal" && strings.ToLower(os.Getenv("CP_GATEWAY_SQL_TRACING")) != "true"
}

func (t *telemetry) tokens(ctx context.Context, k *apiKey, r *route, prompt, completion int) {
	for kind, count := range map[string]int{"prompt": prompt, "completion": completion} {
		if count > 0 {
			t.tokenCount.Add(ctx, int64(count), metric.WithAttributes(attribute.String("project", r.Project), attribute.String("endpoint", r.Endpoint), attribute.String("caller", k.Name), attribute.String("direction", kind)))
		}
	}
}
