"""Make the suite independent of the developer's .env.

agent.loop resolves its provider at import time from .env. With only a Gemini key there, the
fake-Claude tests would run as Gemini (free tier, $0 cost) and fail their cost assertions.
load_dotenv never overrides variables that are already set, so pinning here wins.

The same goes for everything else .env can change: no Postgres (nothing is written to the demo
database; RAG uses the local index, build it first with `python -m rag.build_index`), no
OWN_NAMES (self-transfer tests set their own), and privacy mode on, as it ships.
"""
import os

os.environ["LLM_PROVIDER"] = "anthropic"
os.environ["DATABASE_URL"] = "postgresql://nobody:nothing@127.0.0.1:1/none"
os.environ["RAG_BACKEND"] = "local"
os.environ["OWN_NAMES"] = ""
os.environ["PRIVACY_MODE"] = "redact"
