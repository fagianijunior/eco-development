# Bugfix Requirements Document

## Introduction

O projeto `eco-development` é uma função Lambda (Serverless Framework, Python 3.11) que executa em horários agendados para desligar (`sleep_time`) e ligar (`adventure_time`) recursos AWS marcados com a tag `ecoFriendly=true`, abrangendo ECS, RDS (instâncias e clusters) e EC2.

A revisão do código (`handler.py`, `serverless.yml`, `README.md`) identificou um conjunto de defeitos que comprometem a correção funcional, a confiabilidade e a observabilidade do serviço. Entre eles: nomes de função inconsistentes com o README, lógica de filtro de tags do RDS que gera falsos negativos, divergência de casing de tags entre ECS e RDS, ausência de paginação em listagens da AWS, perda da contagem desejada original de serviços ECS no ciclo de desligamento/ligamento, agendamentos cron que executam em UTC em vez do fuso pretendido, ausência de tratamento de erros, uso de `print` em vez de logging estruturado, permissões IAM desalinhadas com o uso real e leitura frágil da variável de ambiente `REGION`.

Este documento especifica, com critérios de aceitação verificáveis, o comportamento defeituoso atual, o comportamento correto esperado e o comportamento que deve ser preservado após a correção.

## Bug Analysis

### Current Behavior (Defect)

O comportamento defeituoso atual, conforme observado no código:

1.1 WHEN o usuário segue o README e invoca a função `wakeup_time` THEN o sistema falha porque não existe função com esse nome registrada em `serverless.yml` (as funções registradas são `sleep_time` e `adventure_time`)

1.2 WHEN uma instância ou cluster RDS possui múltiplas tags e apenas uma delas é `ecoFriendly=true` THEN o sistema percorre a lista tag a tag e, para cada tag diferente de `ecoFriendly=true`, imprime "is not part of autoshutdown", gerando mensagens contraditórias e avaliação incorreta do recurso

1.3 WHEN uma instância ou cluster RDS possui a tag `ecoFriendly=true` acompanhada de outras tags THEN o sistema pode registrar o recurso simultaneamente como elegível e como "not part of autoshutdown" por causa da decisão tomada dentro do loop de tags, em vez de após verificar a lista inteira

1.4 WHEN o código avalia tags de ECS THEN o sistema usa as chaves `tag["key"]` e `tag["value"]` (minúsculas), enquanto para RDS usa `tag['Key']` e `tag['Value']` (maiúsculas), sem um helper comum, tornando a lógica frágil e inconsistente

1.5 WHEN `ecs.list_clusters`, `ecs.list_services`, `rds.describe_db_instances`, `rds.describe_db_clusters` ou `ec2.describe_instances` retornam resultados paginados THEN o sistema processa apenas a primeira página e ignora silenciosamente os recursos restantes

1.6 WHEN um serviço ECS é religado por `wakeup_ecs_all` THEN o sistema define `desiredCount=1` fixo, descartando a contagem desejada original que o serviço tinha antes do desligamento (`desiredCount=0`)

1.7 WHEN os agendamentos `cron(0 7 ? * MON-FRI *)` e `cron(0 23 ? * MON-FRI *)` são combinados com a env var `TZ=America/Fortaleza` THEN o sistema executa os gatilhos em UTC, pois a variável `TZ` não afeta o agendamento do EventBridge, dando a falsa impressão de horário local

1.8 WHEN qualquer chamada boto3 falha durante o processamento de um recurso THEN o sistema propaga a exceção e interrompe o processamento de todos os demais recursos, pois nenhuma chamada está protegida por tratamento de erros

1.9 WHEN o sistema registra eventos de execução THEN o sistema usa `print`, produzindo logs sem níveis de severidade e sem contexto estruturado para o CloudWatch

1.10 WHEN a política IAM é aplicada THEN o sistema concede `ecs:StartTask` e `ecs:StopTask`, que não são usados pela lógica (que opera via `ecs:UpdateService`), mantendo permissões desalinhadas com o uso real

1.11 WHEN a variável de ambiente `REGION` não está definida THEN o sistema lança `KeyError` já na importação do módulo, ao avaliar `os.environ['REGION']`, impedindo a execução da função

### Expected Behavior (Correct)

O comportamento correto esperado após a correção:

2.1 WHEN o usuário segue o README para invocar as funções THEN o sistema SHALL expor nomes de função consistentes entre `serverless.yml`, `handler.py` e `README.md`, de modo que todos os comandos documentados correspondam a funções realmente registradas

2.2 WHEN uma instância ou cluster RDS possui múltiplas tags e `ecoFriendly=true` está presente entre elas THEN o sistema SHALL avaliar a lista completa de tags e decidir uma única vez que o recurso é elegível, sem emitir mensagens de "not part of autoshutdown" para as demais tags

2.3 WHEN uma instância ou cluster RDS não possui a tag `ecoFriendly=true` em nenhuma de suas tags THEN o sistema SHALL classificá-lo uma única vez como fora do grupo de autoshutdown, após inspecionar a lista inteira

2.4 WHEN o código avalia tags de ECS, RDS ou EC2 THEN o sistema SHALL usar um mecanismo comum de verificação de tags que trate corretamente o casing de cada serviço (`key`/`value` para ECS, `Key`/`Value` para RDS) sem duplicar lógica inconsistente

2.5 WHEN `ecs.list_clusters`, `ecs.list_services`, `rds.describe_db_instances`, `rds.describe_db_clusters` ou `ec2.describe_instances` retornam resultados paginados THEN o sistema SHALL percorrer todas as páginas e processar todos os recursos correspondentes

2.6 WHEN um serviço ECS é desligado e posteriormente religado THEN o sistema SHALL restaurar a contagem desejada original que o serviço tinha antes do desligamento, em vez de assumir `desiredCount=1`

2.7 WHEN os agendamentos são configurados THEN o sistema SHALL disparar nos horários locais pretendidos para `America/Fortaleza`, seja ajustando as expressões cron para o deslocamento UTC correto, seja usando um recurso de agendamento com suporte a fuso horário, de forma que o horário efetivo corresponda ao horário local documentado

2.8 WHEN uma chamada boto3 falha durante o processamento de um recurso THEN o sistema SHALL tratar o erro, registrá-lo e continuar o processamento dos demais recursos sem interromper toda a execução

2.9 WHEN o sistema registra eventos de execução THEN o sistema SHALL usar logging estruturado com níveis de severidade apropriados e contexto suficiente para rastreamento no CloudWatch

2.10 WHEN a política IAM é aplicada THEN o sistema SHALL conceder apenas as permissões efetivamente utilizadas pela lógica, removendo `ecs:StartTask` e `ecs:StopTask` e mantendo `ecs:UpdateService` e as demais ações em uso

2.11 WHEN a variável de ambiente `REGION` não está definida THEN o sistema SHALL recorrer à região padrão do runtime em vez de lançar `KeyError`, permitindo a execução da função

### Unchanged Behavior (Regression Prevention)

O comportamento existente que deve ser preservado após a correção:

3.1 WHEN um recurso ECS, RDS ou EC2 possui a tag `ecoFriendly=true` THEN o sistema SHALL CONTINUE TO selecioná-lo para desligamento no ciclo de sleep e para religamento no ciclo de wakeup

3.2 WHEN um recurso não possui a tag `ecoFriendly=true` THEN o sistema SHALL CONTINUE TO ignorá-lo, sem desligá-lo nem religá-lo

3.3 WHEN uma instância RDS é uma Read Replica ou possui Read Replicas associadas THEN o sistema SHALL CONTINUE TO pulá-la, sem tentar parar ou iniciar o banco

3.4 WHEN uma instância RDS usa engine `aurora-mysql` ou `aurora-postgresql` THEN o sistema SHALL CONTINUE TO tratá-la como cluster Aurora e não como instância isolada, preservando a separação atual entre `describe_db_instances` e `describe_db_clusters`

3.5 WHEN uma instância ou cluster RDS elegível está no estado `available` durante o sleep THEN o sistema SHALL CONTINUE TO pará-lo, e quando está `stopped` durante o wakeup SHALL CONTINUE TO iniciá-lo; estados intermediários (`starting`, `stopping`) SHALL CONTINUE TO ser apenas registrados sem ação

3.6 WHEN uma instância EC2 com `ecoFriendly=true` é processada THEN o sistema SHALL CONTINUE TO pará-la no sleep e iniciá-la no wakeup

3.7 WHEN os ciclos são disparados THEN o sistema SHALL CONTINUE TO executar `sleep_time` para desligar e a função de wakeup para ligar, cobrindo RDS, ECS e EC2 em cada ciclo
