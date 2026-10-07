package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Runs only against the explicitly supplied existing acceptance PostgreSQL fixture.
// Credentials are file-backed, never included in test logs or CLI arguments.
func TestPostgresContract(t *testing.T) {
	path := os.Getenv("CP_GO_TEST_CONFIG")
	if path == "" {
		t.Skip("explicit private acceptance config required")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private test config unreadable")
	}
	var cfg struct{ Admin, Gateway string }
	if json.Unmarshal(data, &cfg) != nil {
		t.Fatal("private test config invalid")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgxpool.New(ctx, cfg.Admin)
	if err != nil {
		t.Fatal("admin pool unavailable")
	}
	defer admin.Close()
	g, _, _ := fixtureGateway()
	a, err := openStore(ctx, cfg.Gateway, 15, g.metrics)
	if err != nil {
		t.Fatal("first pool unavailable")
	}
	defer a.pool.Close()
	b, err := openStore(ctx, cfg.Gateway, 15, g.metrics)
	if err != nil {
		t.Fatal("second pool unavailable")
	}
	defer b.pool.Close()
	if a.Ready(ctx) != nil {
		t.Fatal("gateway role/schema readiness failed")
	}
	suffix := make([]byte, 8)
	_, _ = rand.Read(suffix)
	prefix := "go-poc-test:" + hex.EncodeToString(suffix)
	defer func() {
		cleanup, done := context.WithTimeout(context.Background(), 3*time.Second)
		defer done()
		_, _ = admin.Exec(cleanup, "DELETE FROM gateway_rate_buckets WHERE name LIKE $1", prefix+"%")
	}()
	buckets := []bucket{{prefix + ":endpoint", 1000}, {prefix + ":caller", 1}}
	var admitted, failed atomic.Int64
	var workers sync.WaitGroup
	for i := 0; i < 100; i++ {
		workers.Add(1)
		go func(i int) {
			defer workers.Done()
			store := a
			if i%2 == 1 {
				store = b
			}
			allowance, err := store.Take(ctx, buckets, 1)
			if err != nil {
				failed.Add(1)
			} else if allowance.Allowed {
				admitted.Add(1)
			}
		}(i)
	}
	workers.Wait()
	if failed.Load() != 0 || admitted.Load() != 1 {
		t.Fatalf("shared admission: admitted=%d errors=%d", admitted.Load(), failed.Load())
	}
	// Charged debt/refund use the same transaction primitive as Python; not used by PoC LLM.
	if _, err = a.operate(ctx, buckets, 5, "charge"); err != nil {
		t.Fatal("charge failed")
	}
	after, err := b.Take(ctx, buckets, 1)
	if err != nil || after.Allowed || after.Reset < 290 {
		t.Fatal("debt was not shared")
	}
	if _, err = b.operate(ctx, buckets, 10, "refund"); err != nil {
		t.Fatal("refund failed")
	}
	after, err = a.Take(ctx, buckets, 1)
	if err != nil || !after.Allowed || after.Remaining != 0 {
		t.Fatal("capped refund failed")
	}
	if _, err = a.Take(ctx, []bucket{{prefix + ":dup", 1}, {prefix + ":dup", 2}}, 1); err == nil {
		t.Fatal("duplicate bucket accepted")
	}
	// A real competing PostgreSQL row lock must fail closed within lock_timeout.
	conn, err := admin.Acquire(ctx)
	if err != nil {
		t.Fatal("lock fixture unavailable")
	}
	defer conn.Release()
	tx, err := conn.Begin(ctx)
	if err != nil {
		t.Fatal("lock fixture begin failed")
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, "SELECT 1 FROM gateway_rate_buckets WHERE name=$1 FOR UPDATE", buckets[1].Name); err != nil {
		t.Fatal("lock fixture unavailable")
	}
	start := time.Now()
	_, err = a.Take(ctx, buckets, 1)
	if err == nil || time.Since(start) > 4*time.Second || time.Since(start) < 1500*time.Millisecond {
		t.Fatal("lock timeout not bounded")
	}
	_ = tx.Rollback(ctx)
	if _, err = a.Take(ctx, buckets, 1); err != nil {
		t.Fatal("pool did not recover after timeout")
	}
	// Schema-aware route load is real and uses the runtime role, not an admin bypass.
	route, err := a.Route(ctx, "acceptance-ca9287f7c0", "function")
	if err != nil || route == nil || route.Kind != "function" || !strings.HasPrefix(route.Ref, "mlp-") {
		t.Fatal("real route lookup failed")
	}
	subject := prefix + ":oidc-user"
	_, err = admin.Exec(ctx, `INSERT INTO memberships(id,project_id,subject,role,created_at,updated_at) VALUES(gen_random_uuid(),$1::uuid,$2,'invoker',clock_timestamp(),clock_timestamp())`, route.ProjectID, subject)
	if err != nil {
		t.Fatal("membership fixture failed")
	}
	defer admin.Exec(context.Background(), "DELETE FROM memberships WHERE project_id=$1::uuid AND subject=$2", route.ProjectID, subject)
	allowed, err := a.Invokable(ctx, route.ProjectID, []string{subject})
	if err != nil || !allowed {
		t.Fatal("OIDC member denied by runtime role")
	}
	allowed, err = a.Invokable(ctx, route.ProjectID, []string{subject + ":outsider"})
	if err != nil || allowed {
		t.Fatal("OIDC outsider allowed")
	}

}
