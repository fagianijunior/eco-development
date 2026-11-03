# eco-development

Função Lambda (Serverless Framework v3, Python 3.11) que roda em horários
agendados para desligar e religar recursos AWS marcados com a tag
`ecoFriendly=true`, cobrindo ECS, RDS (instâncias e clusters) e EC2.

Funções registradas (`serverless.yml`):

- `sleep_time` — desliga os recursos.
- `adventure_time` — religa os recursos.

## Setup

O Serverless Framework é instalado localmente via npm (não é global).

```bash
npm install
```

Isso instala o `serverless@3` em `node_modules/`. Use via `npx serverless ...`
ou pelos scripts npm abaixo.

## Testar localmente

Exporte as credenciais AWS no ambiente antes de invocar.

```bash
npx serverless invoke local --function sleep_time       # desligar
npx serverless invoke local --function adventure_time   # religar
```

Ou pelos scripts npm:

```bash
npm run sleep    # invoke local sleep_time
npm run wakeup   # invoke local adventure_time
```

## Deploy

Informe o profile AWS no qual o stack deve ser criado. Sem o profile, o deploy
usa a conta/credenciais default.

```bash
AWS_SDK_LOAD_CONFIG=1 npx serverless deploy --aws-profile <PROFILE>
```

Ou pelo script npm (argumentos extras vão depois de `--`):

```bash
npm run deploy -- --aws-profile <PROFILE>
```

O script `deploy` já define `AWS_SDK_LOAD_CONFIG=1`, necessário para o
Serverless ler profiles configurados em `~/.aws/config`.

## Depois do deploy: marcar os recursos

O deploy sobe a Lambda e os agendamentos, mas por si só **ela não desliga nem
liga nada**. A função só atua sobre recursos que tenham a tag:

| Chave         | Valor  |
| ------------- | ------ |
| `ecoFriendly` | `true` |

Recursos sem essa tag (ou com valor diferente de `true`) são ignorados. Marque
os recursos que devem entrar no ciclo de economia:

### EC2

```bash
aws ec2 create-tags \
  --resources <INSTANCE_ID> \
  --tags Key=ecoFriendly,Value=true \
  --profile <PROFILE>
```

### RDS (instância)

```bash
aws rds add-tags-to-resource \
  --resource-name <DB_INSTANCE_ARN> \
  --tags Key=ecoFriendly,Value=true \
  --profile <PROFILE>
```

### RDS (cluster Aurora)

```bash
aws rds add-tags-to-resource \
  --resource-name <DB_CLUSTER_ARN> \
  --tags Key=ecoFriendly,Value=true \
  --profile <PROFILE>
```

### ECS

A tag vai no **cluster** ECS. A Lambda percorre os clusters marcados e ajusta o
`desiredCount` dos serviços dentro deles.

```bash
aws ecs tag-resource \
  --resource-arn <CLUSTER_ARN> \
  --tags key=ecoFriendly,value=true \
  --profile <PROFILE>
```

> Observação sobre casing: a API do ECS usa `key`/`value` em minúsculas,
> enquanto EC2 e RDS usam `Key`/`Value`. É só uma diferença da CLI de cada
> serviço — o valor da tag é sempre `ecoFriendly=true`.

### Observações

- **RDS Read Replicas** são ignoradas, mesmo com a tag: a Lambda não para nem
  inicia réplicas de leitura.
- Recursos em estados intermediários (`starting`, `stopping`) são apenas
  registrados no log, sem ação.

## Horários de execução

Definidos no `serverless.yml`, nas expressões `schedule` de cada função. O cron
do EventBridge é **sempre avaliado em UTC**, então os horários lá estão
convertidos de America/Fortaleza (UTC-3):

| Função            | Ação      | Horário local (Fortaleza) | Cron (UTC)                |
| ----------------- | --------- | ------------------------- | ------------------------- |
| `adventure_time`  | Ligar     | 07:00, seg-sex            | `cron(0 10 ? * MON-FRI *)`|
| `sleep_time`      | Desligar  | 17:00, seg-sex            | `cron(0 20 ? * MON-FRI *)`|

Para mudar os horários, ajuste as expressões cron no `serverless.yml` somando 3
horas ao horário local desejado e faça o deploy novamente.
