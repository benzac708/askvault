# AskVault Configuration

## Config versus credential

Provider, model and base URL are configuration and travel in a ConfigMap.
The provider key is a credential and travels in a Kubernetes Secret, injected
out of band, never in Git. The Secret reference and ConfigMap references are
all non-optional, so a missing value fails the pod at startup instead of
silently degrading. A missing key can never make the app answer from the mock
provider unseen.

## Production values

In prod the provider is openrouter, the model is pinned to
google/gemini-2.5-flash-lite, and the base URL is the OpenRouter API. The
model is pinned deliberately; a floating free tier would be indistinguishable
from an outage. Locally the default provider is mock, which needs no key at
all and lets the test suite run offline.

## Limits

Rate ceilings are ten per minute per IP, fifteen per minute globally and
forty per day globally. The app enforces its own budget before the provider's
quota is reached and returns a clean 429.