# Models, licenses, and costs

The public prototype is configured for Groq's OpenAI-compatible endpoint and openai/gpt-oss-120b. The model
source is described at https://openai.com/open-models/ and the hosted model page is
https://console.groq.com/docs/model/openai/gpt-oss-120b. The model license is distinct from Groq's hosted
service terms. Groq's 2026-09-24 listed rates are USD 0.15 per 1M input tokens, USD 0.075 per 1M cached
input tokens, and USD 0.60 per 1M output tokens. These are list rates, not measured cost or a billing guarantee.

Local retrieval uses hashing vectors and BM25; no trained embedding model is downloaded. The application is
MIT. Runtime metadata records FastAPI, DuckDB, Pydantic/settings, AnyIO, Uvicorn, pandas, requests, and
numpy under permissive MIT/BSD/Apache-compatible licenses; pypdf is BSD-3-Clause. Numpy also includes
components under 0BSD, MIT, Zlib, and CC0 terms. Review installed package metadata before redistribution.

No QuickBooks credentials are provided and the public connector is disabled. No paid cloud product or signup
is performed by this repository.
