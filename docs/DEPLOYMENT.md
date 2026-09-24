# Deployment

The current shape is Vercel FastAPI plus an optional Cloudflare Worker. The public prototype is configured
with DB_PATH=:memory:, AUTO_REINDEX=false, PUBLIC_DEMO=true, EMBEDDING_PROVIDER=local,
OPENAI_BASE_URL=https://api.groq.com/openai/v1, and OPENAI_MODEL=openai/gpt-oss-120b. The provider key is
an environment secret. No deployment is claimed here; final live checks and hardware validation remain pending.

The worker is restricted to the exact gopalbagaswar.com/rag-assistant and
gopalbagaswar.com/rag-assistant/* routes. It keeps the site root unchanged, redirects the base path to its
trailing-slash form, and forwards only GET, HEAD, POST, and OPTIONS to the fixed HTTPS Vercel origin. It strips
credential and spoofed forwarding headers, preserves safe 4xx responses, sanitizes upstream 5xx/fetch failures,
and rejects upstream redirects. Verify route conflicts and the Vercel origin before any DNS or Worker change.

The public mode uses synthetic data only, is stateless, has no QuickBooks refresh, and exposes no private FLC
documents. Questions, retrieved text, and tool results leave the host when sent to Groq. For local operation,
use make setup, make sample-data, make test, and make run, then run the evaluator described in OPERATIONS.md.
