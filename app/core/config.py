from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "askvault"
    log_level: str = "INFO"

    llm_provider: str = "mock"
    llm_model: str = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    # Bounded because the app is public (D38). Without a timeout a hung provider
    # holds a worker thread until the client gives up, and the limiter admits
    # fresh requests the whole time.
    llm_timeout_seconds: float = 30.0

    samples_dir: str = "samples"
    db_path: str = "data/askvault.db"

    # D32. All three sit strictly under OpenRouter's 20 rpm / 50 per day, so the
    # app exhausts its own budget first and returns a clean 429 instead of
    # leaking a provider error. `tests/test_limits.py` pins this relationship.
    rate_limit_enabled: bool = True
    rate_limit_per_ip_per_minute: int = 10
    rate_limit_global_per_minute: int = 15
    rate_limit_global_per_day: int = 40

    # Traefik is the only thing talking to this app in-cluster, so every request
    # arrives from one peer address. Per-IP limiting therefore depends on
    # X-Forwarded-For, which a client can spoof to dodge the per-IP ceiling.
    # The spoofable ceiling is the fine one; the global ceilings and the
    # provider's own limits are what actually protect the quota. Flipping this
    # to false collapses per-IP limiting into a single shared bucket, which is
    # strictly safer but wrong for legitimate multi-user traffic.
    trust_forwarded_for: bool = True

    # L2. The Ingress routes only `/` and `/chat`, so /metrics is unreachable
    # from outside the cluster. Scraped in-cluster by the ServiceMonitor.
    metrics_enabled: bool = True


settings = Settings()
