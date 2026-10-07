WITH input AS MATERIALIZED (
                        SELECT * FROM jsonb_to_recordset(CAST($1 AS jsonb))
                        AS x(name text, capacity float8)
                    ), locked AS MATERIALIZED (
                        SELECT b.name, b.tokens, b.updated_at FROM gateway_rate_buckets b
                        JOIN input i ON i.name=b.name ORDER BY b.name COLLATE "C" FOR UPDATE OF b
                    ), clock AS MATERIALIZED (
                        SELECT extract(epoch FROM clock_timestamp())::float8 AS now
                        FROM (SELECT count(*) AS n FROM locked) barrier WHERE n=$2
                    ), levels AS MATERIALIZED (
                        SELECT l.name, i.capacity,
                            LEAST(i.capacity, l.tokens +
                                GREATEST(0, c.now-l.updated_at)*i.capacity/60) AS tokens
                        FROM locked l JOIN input i ON i.name=l.name CROSS JOIN clock c
                    ), decision AS MATERIALIZED (
                        SELECT CASE WHEN $3 IN ('take', 'admit')
                            THEN bool_and(tokens >= $4) ELSE true END AS allowed FROM levels
                    )
                    UPDATE gateway_rate_buckets b SET
                        tokens=CASE
                            WHEN ($3='take' AND d.allowed) OR $3='charge'
                                THEN l.tokens-$5
                            WHEN $3='refund' THEN LEAST(l.capacity, l.tokens+$5)
                            ELSE l.tokens END,
                        updated_at=c.now
                    FROM levels l CROSS JOIN decision d CROSS JOIN clock c
                    WHERE b.name=l.name
                    RETURNING b.name, b.tokens, l.capacity, d.allowed
