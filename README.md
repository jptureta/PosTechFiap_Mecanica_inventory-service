# Estoque e Catálogo (`inventory-service`)

Microsserviço independente responsável pelo catálogo de peças e serviços de mão de obra e pelo controle transacional de reserva e liberação de estoque com bloqueio pessimista (`SELECT ... FOR UPDATE`).

---

## 1. Arquitetura e Participação na Saga (SQS)

- **Banco de Dados Exclusivo**: AWS RDS PostgreSQL 16 (`infra/main.tf`).
- **Consumo de Comandos (`oficina-inventory-commands-<env>.fifo` + DLQ)**:
  - `ReserveStock`: Reserva peças de forma atômica e idempotente por `order_id`, emitindo `StockReserved` ou `StockReservationFailed` na fila `os-events.fifo`.
  - `ReleaseStock`: Libera peças reservadas (compensação de cancelamento na execução) e emite `StockReleased`.
- **Consulta Síncrona de Catálogo**: `GET /internal/parts/{sku}` e `GET /internal/services/{sku}` para validação de preços e itens ativos na abertura da OS.

---

## 2. Endpoints e Especificação OpenAPI

A especificação OpenAPI completa está em [`openapi.json`](./openapi.json) e em `/docs`.

- `PUT /parts/{sku}` e `GET /parts/{sku}` — Cadastro/atualização e consulta de peças.
- `PUT /services/{sku}` — Cadastro/atualização de serviços de mão de obra.
- `POST /reservations` e `DELETE /reservations/{order_id}` — Reserva e liberação idempotentes de estoque.
- `GET /health`, `GET /health/live`, `GET /metrics` — Healthchecks e métricas com `X-Correlation-ID`.

---

## 3. Testes, Cobertura e CI/CD

```bash
uv sync --dev
uv run pytest --cov=app --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

### Evidência de Cobertura de Testes (89% > 80% exigido)

```text
Name                   Stmts   Miss  Cover
------------------------------------------
app/__init__.py            0      0   100%
app/auth.py               26      0   100%
app/main.py              207     24    88%
app/messaging.py          58     13    78%
app/observability.py      58      1    98%
------------------------------------------
TOTAL                    349     38    89%
6 passed
```

O workflow `.github/workflows/ci-cd.yml` valida testes + cobertura >= 80%, executa scan no **SonarCloud**, valida o Terraform (`infra/`) e realiza deploy automatizado no **Amazon EKS**.
