"""Make the suite independent of the developer's .env.

agent.loop resolves its provider at import time from .env. With only a Gemini key there, the
fake-Claude tests would run as Gemini (free tier, $0 cost) and fail their cost assertions.
load_dotenv never overrides variables that are already set, so pinning here wins.
"""
import os

os.environ["LLM_PROVIDER"] = "anthropic"
