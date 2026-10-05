"""Database-enforced append-only audit records (PostgreSQL)."""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE FUNCTION public.mlp_audit_append_only() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = '42501';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER mlp_audit_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON public.audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION public.mlp_audit_append_only()
    """)
    op.execute("REVOKE ALL ON FUNCTION public.mlp_audit_append_only() FROM PUBLIC")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER mlp_audit_append_only ON public.audit_events")
        op.execute("DROP FUNCTION public.mlp_audit_append_only()")
