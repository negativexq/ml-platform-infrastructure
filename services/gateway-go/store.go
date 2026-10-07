package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed limiter.sql
var limiterSQL string

type store struct {
	pool    *pgxpool.Pool
	metrics *telemetry
}

func openStore(ctx context.Context, dsn string, capacity int, t *telemetry) (*store, error) {
	dsn = strings.Replace(dsn, "postgresql+psycopg://", "postgresql://", 1)
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, errors.New("invalid database configuration")
	}
	cfg.MaxConns = int32(capacity)
	cfg.MinConns = 0
	cfg.ConnConfig.ConnectTimeout = 3 * time.Second
	// No extra statement-cache optimization in this runtime comparison.
	cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeExec
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, errors.New("database pool creation failed")
	}
	t.observePool(pool)
	return &store{pool, t}, nil
}
func (s *store) acquire(ctx context.Context) (*pgxpool.Conn, error) {
	started := time.Now()
	acquireCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	c, err := s.pool.Acquire(acquireCtx)
	// Match Python pool_pre_ping instead of skipping its per-checkout health query.
	if err == nil {
		err = c.Ping(acquireCtx)
		if err != nil {
			c.Release()
			c = nil
		}
	}
	s.metrics.database(ctx, "acquire", started, err)
	return c, err
}
func (s *store) Key(ctx context.Context, id string) (*apiKey, error) {
	c, err := s.acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer c.Release()
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	k := &apiKey{}
	var endpoints []byte
	started := time.Now()
	err = c.QueryRow(ctx, `SELECT key_id,project_id::text,name,secret_hash,endpoints,units_per_minute,expires_at,revoked_at FROM api_keys WHERE key_id=$1`, id).Scan(&k.ID, &k.ProjectID, &k.Name, &k.Hash, &endpoints, &k.Limit, &k.Expires, &k.Revoked)
	if errors.Is(err, pgx.ErrNoRows) {
		err = nil
		s.metrics.database(ctx, "query", started, err)
		return nil, nil
	}
	s.metrics.database(ctx, "query", started, err)
	if err != nil {
		return nil, err
	}
	if err = json.Unmarshal(endpoints, &k.Endpoints); err != nil {
		return nil, err
	}
	return k, nil
}
func (s *store) Route(ctx context.Context, project, endpoint string) (*route, error) {
	c, err := s.acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer c.Release()
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	r := &route{}
	started := time.Now()
	err = c.QueryRow(ctx, `SELECT p.id::text,p.name,e.name,e.status,e.kind,e.protocol,coalesce(e.url,''),
 'mlp-'||p.name||'/'||d.name,e.limit_units_per_minute,e.limit_max_body_kb,e.limit_timeout_seconds,
 CASE WHEN d.active_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM rollouts ro WHERE ro.deployment_id=d.id AND ro.status IN ('PENDING','PROGRESSING')) THEN coalesce(m.name||' v'||mv.version::text,'') ELSE '' END
 FROM projects p JOIN endpoints e ON e.project_id=p.id JOIN deployments d ON d.id=e.deployment_id
 LEFT JOIN deployment_revisions dr ON dr.deployment_id=d.id AND dr.revision=d.active_revision
 LEFT JOIN model_versions mv ON mv.id=dr.model_version_id LEFT JOIN models m ON m.id=mv.model_id
 WHERE p.name=$1 AND e.name=$2 AND e.exposure='public'`, project, endpoint).Scan(&r.ProjectID, &r.Project, &r.Endpoint, &r.Status, &r.Kind, &r.Protocol, &r.URL, &r.Ref, &r.Limit, &r.MaxBodyKB, &r.Timeout, &r.Model)
	if errors.Is(err, pgx.ErrNoRows) {
		err = nil
		s.metrics.database(ctx, "query", started, err)
		return nil, nil
	}
	s.metrics.database(ctx, "query", started, err)
	if err != nil {
		return nil, err
	}
	return r, nil
}
func (s *store) Touch(ctx context.Context, id string) error {
	c, err := s.acquire(ctx)
	if err != nil {
		return err
	}
	defer c.Release()
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	started := time.Now()
	_, err = c.Exec(ctx, "UPDATE api_keys SET last_used_at=clock_timestamp() WHERE key_id=$1", id)
	s.metrics.database(ctx, "query", started, err)
	return err
}
func (s *store) Take(ctx context.Context, b []bucket, units int) (allowance, error) {
	return s.operate(ctx, b, units, "take")
}
func (s *store) operate(ctx context.Context, b []bucket, units int, op string) (a allowance, err error) {
	started := time.Now()
	defer func() { s.metrics.limiter(ctx, started, a, err) }()
	if len(b) == 0 || units < 0 || (op != "take" && op != "admit" && op != "refund" && op != "charge") {
		return a, errors.New("invalid rate-limit operation")
	}
	b = append([]bucket(nil), b...)
	sort.Slice(b, func(i, j int) bool { return b[i].Name < b[j].Name })
	for i, v := range b {
		if v.Capacity <= 0 || (i > 0 && v.Name == b[i-1].Name) {
			return a, errors.New("invalid rate-limit buckets")
		}
	}
	payload, _ := json.Marshal(b)
	ctx, cancel := context.WithTimeout(ctx, 9*time.Second)
	defer cancel()
	c, err := s.acquire(ctx)
	if err != nil {
		return a, err
	}
	defer c.Release()
	tx, err := c.Begin(ctx)
	if err != nil {
		return a, err
	}
	transactionStart := time.Now()
	defer func() {
		cleanup, stop := context.WithTimeout(context.Background(), 3*time.Second)
		defer stop()
		_ = tx.Rollback(cleanup)
	}()
	exec := func(sql string, args ...any) error {
		t := time.Now()
		_, e := tx.Exec(ctx, sql, args...)
		s.metrics.database(ctx, "query", t, e)
		return e
	}
	if err = exec("SELECT set_config('lock_timeout', '2s', true), set_config('statement_timeout', '3s', true)"); err != nil {
		return a, err
	}
	if err = exec(`INSERT INTO gateway_rate_buckets (name,tokens,updated_at) SELECT name,capacity,0 FROM jsonb_to_recordset($1::jsonb) AS x(name text,capacity float8) ORDER BY name COLLATE "C" ON CONFLICT (name) DO NOTHING`, string(payload)); err != nil {
		return a, err
	}
	cost := units
	if op != "take" {
		cost = 1
	}
	queryStart := time.Now()
	rows, err := tx.Query(ctx, limiterSQL, string(payload), len(b), op, cost, units)
	if err != nil {
		s.metrics.database(ctx, "query", queryStart, err)
		return a, err
	}
	levels := map[string]float64{}
	allowed := false
	for rows.Next() {
		var name string
		var tokens, capacity float64
		if err = rows.Scan(&name, &tokens, &capacity, &allowed); err != nil {
			break
		}
		levels[name] = tokens
	}
	if err == nil {
		err = rows.Err()
	}
	rows.Close()
	s.metrics.database(ctx, "query", queryStart, err)
	if err != nil {
		return a, err
	}
	if len(levels) != len(b) {
		return a, errors.New("rate-limit bucket set incomplete")
	}
	s.metrics.database(ctx, "transaction", transactionStart, nil)
	if err = tx.Commit(ctx); err != nil {
		return a, err
	}
	a.Allowed = allowed
	if !allowed {
		wait := -1.0
		for _, v := range b {
			if levels[v.Name] < float64(cost) {
				delay := (float64(cost) - levels[v.Name]) * 60 / float64(v.Capacity)
				if delay > wait {
					wait = delay
					a.Limit = v.Capacity
					a.Reset = int(math.Ceil(delay))
				}
			}
		}
		return a, nil
	}
	smallest := math.Inf(1)
	for _, v := range b {
		if levels[v.Name] < smallest {
			smallest = levels[v.Name]
			a.Limit = v.Capacity
			a.Remaining = max(0, int(smallest))
			a.Reset = int(math.Ceil((float64(v.Capacity) - smallest) * 60 / float64(v.Capacity)))
		}
	}
	return a, nil
}
func (s *store) Ready(ctx context.Context) error {
	c, err := s.acquire(ctx)
	if err != nil {
		return err
	}
	defer c.Release()
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	var head string
	if err = c.QueryRow(ctx, "SELECT CASE WHEN count(*)=1 THEN min(version_num) ELSE '' END FROM alembic_version").Scan(&head); err != nil {
		return err
	}
	// 0022 adds scheduling tables without changing gateway queries or privileges.
	// Accept both heads so this gateway can roll out before the additive migration.
	if head != "0021" && head != "0022" {
		return errors.New("schema does not match this release")
	}
	var safe bool
	err = c.QueryRow(ctx, `SELECT rolcanlogin AND current_user=session_user AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
 AND pg_has_role(current_user,'mlp_gateway','USAGE') AND NOT has_schema_privilege(current_user,'public','CREATE')
 AND NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid WHERE m.member=pg_roles.oid AND p.rolname<>'mlp_gateway')
 AND NOT EXISTS (SELECT 1 FROM pg_class WHERE relowner=pg_roles.oid)
 AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=pg_roles.oid)
 AND NOT EXISTS (SELECT 1 FROM pg_database WHERE datdba=pg_roles.oid)
 AND NOT has_table_privilege(current_user,'public.audit_events','UPDATE,DELETE,TRUNCATE')
 AND NOT has_table_privilege(current_user,'public.audit_events','SELECT')
 AND NOT has_table_privilege(current_user,'public.job_definitions','SELECT')
 AND NOT has_table_privilege(current_user,'public.memberships','INSERT,DELETE')
 AND NOT has_column_privilege(current_user,'public.api_keys','endpoints','UPDATE')
 AND NOT has_column_privilege(current_user,'public.api_keys','units_per_minute','UPDATE')
 AND NOT has_column_privilege(current_user,'public.api_keys','expires_at','UPDATE')
 AND NOT has_column_privilege(current_user,'public.api_keys','revoked_at','UPDATE')
 AND NOT has_table_privilege(current_user,'public.memberships','UPDATE')
 AND NOT has_column_privilege(current_user,'public.api_keys','secret_hash','UPDATE')
 FROM pg_roles WHERE rolname=current_user`).Scan(&safe)
	if err != nil {
		return err
	}
	if !safe {
		return fmt.Errorf("unsafe gateway database role")
	}
	return nil
}

func (s *store) Invokable(ctx context.Context, project string, subjects []string) (bool, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	c, err := s.acquire(ctx)
	if err != nil {
		return false, err
	}
	defer c.Release()
	var allowed bool
	err = c.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM memberships WHERE project_id=$1::uuid AND subject=ANY($2::text[]) AND role IN ('invoker','viewer','operator','admin'))`, project, subjects).Scan(&allowed)
	return allowed, err
}
