# Estoque e Catálogo

Serviço independente para cadastro de peças e reserva/liberação de estoque.
Possui PostgreSQL exclusivo; OS consome somente a API HTTP e nunca acessa esse
banco diretamente.

Execute `docker compose up --build` e acesse `http://localhost:8003/docs`.
Testes: `uv sync --dev && uv run pytest`.
Para execução local fora do Docker, rode `alembic upgrade head` antes de iniciar
o servidor.

- `PUT /parts/{sku}` cadastra/atualiza o saldo disponível.
- `PUT /services/{sku}` cadastra/atualiza serviços e preços de mão de obra.
- `POST /reservations` reserva quantidades atomicamente e de forma idempotente
  por `order_id`.
- `DELETE /reservations/{order_id}` libera a reserva (compensação idempotente).

Configure `INTERNAL_SERVICE_TOKEN` com o mesmo valor do OS. O serviço deve
permanecer privado, acessível apenas pelos serviços autorizados.
`JWT_SECRET_KEY`/`JWT_ALGORITHM` devem corresponder ao emissor legado para
leituras de catálogo autenticadas.
O namespace também precisa do secret `ghcr-pull` para leitura de imagens privadas
no GHCR.

Com `MESSAGING_ENABLED=true`, o serviço consome `ReserveStock` e `ReleaseStock`
da fila durável `oficina.inventory.commands` e publica `StockReserved`,
`StockReservationFailed` ou `StockReleased` em `oficina.os.events`. Configure
`RABBITMQ_URL`; em Kubernetes, o deployment usa o secret
`microservices-secrets`, chave `rabbitmq-url`. O serviço acessa somente seu
próprio PostgreSQL; o OS consulta o catálogo pela API HTTP e nunca acessa o banco
do Inventory.

O RDS exclusivo é definido em `infra/`. Valide com
`terraform -chdir=infra init -backend=false`, `terraform -chdir=infra validate`
e `terraform -chdir=infra plan` após fornecer as variáveis requeridas. O apply
exige backend de estado remoto e revisão do plano antes de provisionar recursos.
