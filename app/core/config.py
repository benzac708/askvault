from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "askvault"
    llm_provider: str = "mock"
    llm_model: str = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    samples_dir: str = "samples"
    db_path: str = "data/askvault.db"


settings = Settings()
