# Eco Scheduler Review Bugfix Design

## Overview

O `eco-development` é uma função Lambda (Serverless Framework, Python 3.11) que, em horários agendados, desliga (`sleep_time`) e religa (`adventure_time`) recursos AWS marcados com a tag `ecoFriendly=true`, cobrindo ECS, RDS (instâncias e clusters) e EC2.

A revisão de `handler.py`, `serverless.yml` e `README.md` expôs um conjunto de defeitos que não são um único bug pontual, e sim uma família de condições de bug que compartilham a mesma superfície (o módulo `handler.py` e sua configuração). A abordagem de correção é, portanto, dividida por área de defeito, cada uma com sua condição de bug formal `isBugCondition`, mantendo intacto o comportamento de seleção por tag e de transição de estado dos recursos.

A estratégia geral:

- Introduzir um **helper comum de verificação de tags** que normaliza o casing (`key`/`value` do ECS e `Key`/`Value` do RDS/EC2) e decide a elegibilidade uma única vez por recurso, após inspecionar a lista inteira.
- **Paginar** todas as listagens boto3 via `get_paginator`.
- **Persistir e restaurar** o `desiredCount` original dos serviços ECS em vez de assumir `1` fixo.
- **Corrigir o agendamento** para refletir o horário local de `America/Fortaleza` (UTC-3), ajustando as expressões cron do EventBridge (que são sempre UTC) ou migrando para o EventBridge Scheduler com timezone.
- **Isolar falhas por recurso** com `try/except`, para que um erro em um recurso não interrompa o processamento dos demais.
- **Substituir `print` por logging estruturado** via módulo `logging`.
- **Alinhar as permissões IAM** ao uso real (remoção de `ecs:StartTask`/`ecs:StopTask`).
- **Ler `REGION` de forma resiliente** com `os.environ.get` e fallback para a região default do runtime.
- **Unificar os nomes de função** entre `serverless.yml`, `handler.py` e `README.md`.

**Arquivos impactados:** `handler.py`, `serverless.yml`, `README.md`.

## Glossary

- **Bug_Condition (C)**: A condição que caracteriza cada defeito. Como esta revisão agrupa múltiplos defeitos na mesma superfície, `isBugCondition` é definida como a disjunção das sub-condições por área (tags, paginação, desiredCount, agendamento, erros, logging, IAM, REGION, nomenclatura).
- **Property (P)**: O comportamento desejado quando a condição de bug é satisfeita — seleção correta por tag, cobertura completa de páginas, restauração da contagem original, disparo no horário local pretendido, resiliência a falhas, logs estruturados, IAM mínimo e leitura resiliente de REGION.
- **Preservation**: O comportamento de seleção por `ecoFriendly=true`, as transições de estado RDS/EC2/ECS e as regras de Read Replica/Aurora que devem permanecer inalterados após a correção.
- **F (função original)**: O `handler.py`/`serverless.yml` atuais, antes da correção.
- **F' (função corrigida)**: O `handler.py`/`serverless.yml` após a correção.
- **filter_by_tags / tag_matches**: Helper comum de verificação de tags em `handler.py` que decide a elegibilidade de um recurso a partir de sua lista completa de tags.
- **desiredCount**: A contagem desejada de tasks de um serviço ECS, usada por `ecs.update_service`.
- **EventBridge cron**: Expressão de agendamento do EventBridge/Scheduler, avaliada **sempre em UTC** independentemente da env var `TZ`.

## Bug Details

### Bug Condition

O bug se manifesta quando qualquer uma das áreas revisadas é exercitada: avaliação de tags RDS com múltiplas tags, avaliação de tags ECS com casing divergente, listagens AWS que excedem uma página, religamento de serviço ECS, disparo de agendamento esperando horário local, ocorrência de falha boto3 em um recurso, emissão de log, aplicação da política IAM, ausência da env var `REGION`, ou uso dos nomes de função documentados no README. A função atual (`F`) trata cada um desses casos de forma incorreta, inconsistente ou frágil.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type SchedulerInvocation
         (recursos AWS + env + config de agendamento + política IAM + nomes documentados)
  OUTPUT: boolean

  RETURN
    // C_tags_rds: RDS com múltiplas tags, ecoFriendly=true presente, decisão tomada tag-a-tag
    (input.rdsResource.tags CONTAINS {Key:"ecoFriendly", Value:"true"}
       AND countTags(input.rdsResource) > 1)
    OR
    // C_tags_casing: avaliação de tags sem helper comum / casing divergente ECS vs RDS
    (input.evaluatesTags AND NOT usesCommonTagHelper())
    OR
    // C_pagination: listagem AWS com mais de uma página
    (listCallIsPaginated(input.listCall) AND NOT usesPaginator(input.listCall))
    OR
    // C_desiredCount: wakeup de serviço ECS cujo desiredCount original != 1
    (input.operation == WAKEUP_ECS
       AND originalDesiredCount(input.ecsService) != 1
       AND wakeupSetsFixedDesiredCount(1))
    OR
    // C_schedule: horário local pretendido difere do horário efetivo em UTC
    (effectiveTriggerTimeUTC(input.schedule) != intendedLocalTime(input.schedule, "America/Fortaleza"))
    OR
    // C_errors: falha boto3 em um recurso interrompe o processamento dos demais
    (boto3CallRaises(input.resource) AND NOT isolatedPerResource(input.resource))
    OR
    // C_logging: emissão de log via print em vez de logging estruturado
    (input.emitsLog AND usesPrint())
    OR
    // C_iam: política concede ação ECS não utilizada
    (input.iamPolicy GRANTS ("ecs:StartTask" OR "ecs:StopTask"))
    OR
    // C_region: REGION ausente provoca KeyError na importação
    (NOT envDefined("REGION") AND readsRegionAsRequired())
    OR
    // C_naming: nome de função documentado não existe em serverless.yml
    (input.documentedFunctionName NOT IN registeredFunctions(serverless.yml))
END FUNCTION
```

### Examples

- **RDS multi-tag (1.2, 1.3):** uma instância com tags `[{Key:"env",Value:"prod"}, {Key:"ecoFriendly",Value:"true"}]`. **Atual (F):** ao iterar a tag `env`, imprime "is not part of autoshutdown"; ao iterar `ecoFriendly`, para a instância — mensagens contraditórias para o mesmo recurso. **Esperado (F'):** inspeciona a lista inteira, decide uma única vez que é elegível, sem mensagem de "not part of autoshutdown".
- **ECS casing (1.4):** `filter_by_tags` lê `tag["key"]`/`tag["value"]`, enquanto o RDS lê `tag['Key']`/`tag['Value']`. **Atual (F):** duas lógicas divergentes; passar uma tag RDS por `filter_by_tags` levantaria `KeyError`. **Esperado (F'):** um único helper que normaliza o casing e funciona para ECS, RDS e EC2.
- **Paginação (1.5):** uma conta com 150 serviços ECS em um cluster. **Atual (F):** `list_services` retorna ~100 e o restante é ignorado silenciosamente. **Esperado (F'):** paginador percorre todas as páginas.
- **desiredCount (1.6):** serviço ECS rodando com `desiredCount=4`. **Atual (F):** sleep define `0`, wakeup define `1` — o serviço volta subdimensionado. **Esperado (F'):** wakeup restaura `4`.
- **Agendamento (1.7):** `cron(0 7 ? * MON-FRI *)` com `TZ=America/Fortaleza`. **Atual (F):** dispara às 07:00 **UTC** (04:00 em Fortaleza). **Esperado (F'):** dispara às 07:00 locais (10:00 UTC) — cron ajustada para `cron(0 10 ? * MON-FRI *)` ou Scheduler com `timezone: America/Fortaleza`.
- **Erro por recurso (1.8):** `rds.stop_db_instance` falha por estado inválido na terceira instância. **Atual (F):** a exceção aborta o ciclo inteiro; ECS e EC2 não são processados. **Esperado (F'):** o erro é registrado e o processamento continua.
- **REGION ausente (1.11 / edge case):** invocação sem `REGION` no ambiente. **Atual (F):** `KeyError` já no import de `handler.py`. **Esperado (F'):** usa a região default do runtime e executa normalmente.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Recursos ECS, RDS e EC2 com `ecoFriendly=true` continuam selecionados para desligar no sleep e religar no wakeup.
- Recursos sem `ecoFriendly=true` continuam ignorados (nem desligados nem religados).
- Instâncias RDS Read Replica, ou com Read Replicas associadas, continuam puladas.
- Engines `aurora-mysql`/`aurora-postgresql` continuam tratadas como cluster Aurora (separação entre `describe_db_instances` e `describe_db_clusters` preservada).
- Transições de estado RDS/cluster preservadas: `available` → para no sleep; `stopped` → inicia no wakeup; `starting`/`stopping` apenas registrados sem ação.
- Instâncias EC2 `ecoFriendly=true` continuam paradas no sleep e iniciadas no wakeup.
- Cada ciclo continua cobrindo RDS, ECS e EC2.

**Scope:**
Todas as entradas que NÃO satisfazem `isBugCondition` devem permanecer completamente inalteradas pela correção. Isso inclui:
- Recursos com `ecoFriendly=true` e uma única tag (seleção já correta hoje).
- Recursos sem a tag `ecoFriendly` (já ignorados).
- Read Replicas e clusters Aurora (já tratados conforme as regras atuais).
- Transições de estado já corretas em RDS e EC2.

**Nota:** O comportamento correto esperado para as entradas com bug está definido na seção Correctness Properties (Property 1). Esta seção foca no que NÃO deve mudar.

## Hypothesized Root Cause

Com base na análise do código, as causas mais prováveis por área são:

1. **Decisão de tag dentro do loop (RDS)**: a elegibilidade do recurso RDS é decidida dentro do `for tag in resp2['TagList']`, com ramos `elif tag['Key'] != ... and tag['Value'] != ...` que executam uma vez por tag não correspondente. A decisão deveria ocorrer **após** varrer a lista inteira (ex.: `any(...)`).
   - O ramo `elif tag['Key']!="ecoFriendly" and tag['Value']!="true"` é logicamente incorreto: usa `and` onde deveria ser `or`, e avalia por tag.

2. **Casing divergente e ausência de helper comum**: `filter_by_tags` assume `key`/`value` (formato ECS), enquanto o fluxo RDS usa `Key`/`Value` inline. Não há normalização.

3. **Ausência de paginação**: as chamadas `list_clusters`, `list_services`, `describe_db_instances`, `describe_db_clusters` e `describe_instances` consomem apenas o primeiro retorno, sem `NextToken`/paginator.

4. **Perda do desiredCount original**: o sleep força `desiredCount=0` sem persistir o valor anterior; o wakeup usa `1` fixo. Não há onde o valor original seja guardado.

5. **cron em UTC**: o EventBridge avalia cron sempre em UTC; a env var `TZ` afeta o runtime Python, não o agendamento. As expressões `0 7` e `0 23` nunca correspondem ao horário local.

6. **Sem tratamento de erros**: nenhuma chamada boto3 está envolta em `try/except`; qualquer exceção propaga e aborta o ciclo.

7. **`print` em vez de `logging`**: logs sem nível de severidade nem contexto estruturado.

8. **IAM com ações não usadas**: a política concede `ecs:StartTask`/`ecs:StopTask`, mas a lógica opera apenas via `ecs:UpdateService`.

9. **Leitura rígida de REGION**: `region = os.environ['REGION']` em escopo de módulo levanta `KeyError` na importação quando a env var não existe.

10. **Nomenclatura divergente**: o README referencia `wakeup_time`, inexistente em `serverless.yml` (que registra `adventure_time`).

## Correctness Properties

Property 1: Bug Condition - Correção abrangente do agendador eco

_For any_ entrada onde a condição de bug se verifica (`isBugCondition` retorna true), a função corrigida SHALL produzir o comportamento correto correspondente à sub-condição acionada: (a) decidir a elegibilidade de um recurso RDS uma única vez após inspecionar a lista inteira de tags, selecionando-o quando `ecoFriendly=true` está presente e sem emitir "not part of autoshutdown" para as demais tags; (b) avaliar tags de ECS, RDS e EC2 por meio de um helper comum que trata o casing de cada serviço; (c) percorrer todas as páginas das listagens AWS e processar todos os recursos; (d) restaurar, no wakeup de ECS, a contagem desejada original anterior ao desligamento; (e) disparar os agendamentos no horário local pretendido para `America/Fortaleza`; (f) tratar falhas boto3 por recurso, registrando e continuando o processamento dos demais; (g) emitir logs estruturados com níveis de severidade; (h) aplicar uma política IAM sem `ecs:StartTask`/`ecs:StopTask`; (i) recorrer à região default do runtime quando `REGION` não está definida; (j) expor nomes de função consistentes entre `serverless.yml`, `handler.py` e `README.md`.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11**

Property 2: Preservation - Seleção por tag e transições de estado inalteradas

_For any_ entrada onde a condição de bug NÃO se verifica (`isBugCondition` retorna false), a função corrigida SHALL produzir o mesmo resultado que a função original, preservando: a seleção de recursos `ecoFriendly=true` para sleep/wakeup, a exclusão de recursos sem a tag, o salto de Read Replicas e de bancos com Read Replicas, o tratamento de engines Aurora como cluster, as transições de estado RDS/cluster (`available`→stop no sleep, `stopped`→start no wakeup, `starting`/`stopping` apenas registrados) e o stop/start de instâncias EC2 `ecoFriendly=true`.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

Assumindo que a análise de causa raiz está correta:

**Arquivo**: `handler.py`

1. **Leitura resiliente de REGION** (2.11): substituir `region = os.environ['REGION']` por `region = os.environ.get("REGION") or os.environ.get("AWS_REGION")`, deixando o boto3 recorrer à região default do runtime quando `REGION` não é definida. Evitar que a leitura ocorra em escopo de módulo de forma que possa quebrar o import; preferir resolver a região dentro das funções ou de forma tolerante a `None`.

2. **Logging estruturado** (2.9): configurar `logger = logging.getLogger()` com `logger.setLevel(logging.INFO)` no topo do módulo e substituir todos os `print(...)` por `logger.info/warning/error(...)` com contexto (identificador do recurso, operação, estado).

3. **Helper comum de tags** (2.4): introduzir uma função única, por exemplo:
   ```
   FUNCTION tag_matches(tags, wanted_key, wanted_value)
     FOR each tag IN tags
       key   := tag.get("Key",   tag.get("key"))
       value := tag.get("Value", tag.get("value"))
       IF key == wanted_key AND value == wanted_value THEN RETURN true
     RETURN false
   ```
   Usá-la em ECS, RDS e EC2 (quando aplicável) no lugar de `filter_by_tags` e das comparações inline `tag['Key']==...`.

4. **Correção da lógica de filtro RDS** (2.2, 2.3): decidir a elegibilidade **uma única vez** por recurso usando o helper (`eligible = tag_matches(tagList, "ecoFriendly", "true")`). Remover os ramos `elif tag[...] != ... and tag[...] != ...` que avaliam tag a tag. Classificar como "fora do autoshutdown" apenas quando `eligible` é falso, após inspecionar a lista inteira. Aplicar o mesmo tratamento a `describe_db_instances` e `describe_db_clusters`, tanto no sleep quanto no wakeup.

5. **Paginação** (2.5): usar `get_paginator` para `list_clusters`, `list_services`, `describe_db_instances`, `describe_db_clusters` e `describe_instances`, iterando todas as páginas e acumulando/processando todos os itens.

6. **Restauração do desiredCount original** (2.6): no sleep de ECS, antes de definir `desiredCount=0`, ler o `desiredCount` atual do serviço (via `describe_services`) e persistir o valor original — preferencialmente como uma tag no serviço (ex.: `ecoOriginalDesiredCount`) via `tag_resource`, para sobreviver entre invocações sem estado externo. No wakeup, ler essa tag e restaurar o valor; na ausência da tag, aplicar um fallback seguro documentado. (Alternativa: armazenar em um backing store como SSM Parameter Store/DynamoDB — a tag no serviço é a opção mínima e sem dependências novas.)

7. **Tratamento de erros por recurso** (2.8): envolver cada operação por recurso (cada serviço ECS, cada instância/cluster RDS, cada instância EC2) em `try/except`, registrando o erro via `logger.error(...)` e seguindo para o próximo recurso, sem propagar a exceção para fora do loop.

**Arquivo**: `serverless.yml`

8. **Ajuste de agendamento** (2.7): converter os horários locais pretendidos de `America/Fortaleza` (UTC-3) para UTC nas expressões cron do EventBridge — `cron(0 7 ...)` → `cron(0 10 ...)` e `cron(0 23 ...)` → `cron(0 2 ... )` no dia seguinte (atenção ao deslocamento de dia da semana) — **ou** migrar para EventBridge Scheduler com `timezone: America/Fortaleza`, mantendo as expressões em horário local. A opção escolhida deve ser documentada para que o horário efetivo corresponda ao documentado. Como Fortaleza não observa horário de verão, o offset UTC-3 é fixo.

9. **IAM mínima** (2.10): remover `ecs:StartTask` e `ecs:StopTask` do bloco de permissões, mantendo `ecs:UpdateService`, `ecs:ListClusters`, `ecs:DescribeClusters`, `ecs:ListServices` e, se a restauração via tag for adotada, adicionar `ecs:DescribeServices`, `ecs:TagResource` e `ecs:ListTagsForResource` conforme necessário.

10. **Consistência de nomes de função** (2.1): alinhar os nomes entre `serverless.yml`, `handler.py` e `README.md`. Definir o nome canônico da função de wakeup (manter `adventure_time` ou renomear para `wakeup_time`) e refletir a mesma escolha nos três arquivos.

**Arquivo**: `README.md`

11. **Documentação consistente** (2.1): corrigir os comandos de invocação para os nomes de função realmente registrados (ex.: substituir `wakeup_time` pelo nome canônico adotado) e descrever a estratégia de agendamento (UTC convertido vs Scheduler com timezone) e a tag de contagem desejada, se aplicável.

## Testing Strategy

### Validation Approach

A estratégia segue duas fases: primeiro, surfacear contraexemplos que demonstram cada defeito no código NÃO corrigido; depois, verificar que a correção funciona e que o comportamento de seleção/transição foi preservado.

### Exploratory Bug Condition Checking

**Goal**: Surfacear contraexemplos que demonstram os bugs ANTES de implementar a correção, confirmando ou refutando a análise de causa raiz. Se refutarmos, re-hipotetizar.

**Test Plan**: Escrever testes que exercitam cada área de defeito com mocks do boto3 (ex.: `botocore.stub.Stubber` ou `moto`), rodando contra o código NÃO corrigido para observar as falhas.

**Test Cases**:
1. **RDS multi-tag**: instância com `[env=prod, ecoFriendly=true]` — assert de que é parada sem emitir "not part of autoshutdown" (vai falhar no código atual).
2. **ECS casing**: passar tags no formato RDS (`Key`/`Value`) pela verificação de ECS — expor o `KeyError`/inconsistência (vai falhar no código atual).
3. **Paginação**: listagem com duas páginas (`NextToken`) — assert de que todos os recursos das duas páginas são processados (vai falhar no código atual).
4. **desiredCount**: serviço com `desiredCount=4` → sleep → wakeup — assert de que volta a `4` (vai falhar no código atual, que volta a `1`).
5. **Erro por recurso**: forçar exceção em um recurso — assert de que os demais ainda são processados (vai falhar no código atual).
6. **REGION ausente** (edge case): importar/invocar sem `REGION` — assert de que não há `KeyError` (vai falhar no código atual).
7. **Agendamento**: validar que a expressão cron em UTC corresponde ao horário local pretendido de Fortaleza (vai falhar com `0 7`/`0 23`).
8. **Nomenclatura**: validar que todo nome de função citado no README existe em `serverless.yml` (vai falhar com `wakeup_time`).

**Expected Counterexamples**:
- RDS elegível classificado também como "not part of autoshutdown".
- Recursos além da primeira página ignorados.
- Serviço ECS religado com `desiredCount=1` em vez do original.
- Causas prováveis: decisão de tag dentro do loop, ausência de paginação, `desiredCount` fixo, falta de `try/except`, leitura rígida de `REGION`.

### Fix Checking

**Goal**: Verificar que, para toda entrada onde a condição de bug se verifica, a função corrigida produz o comportamento esperado.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT expectedBehavior(result)
END FOR
```

### Preservation Checking

**Goal**: Verificar que, para toda entrada onde a condição de bug NÃO se verifica, a função corrigida produz o mesmo resultado que a função original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Teste baseado em propriedades é recomendado para a verificação de preservação porque:
- Gera muitos casos automaticamente ao longo do domínio de entrada.
- Captura casos de borda que testes manuais podem perder.
- Fornece garantias fortes de que o comportamento permanece inalterado para todas as entradas não defeituosas.

**Test Plan**: Observar o comportamento no código NÃO corrigido para entradas sem bug (recurso `ecoFriendly=true` com tag única, recurso sem a tag, Read Replica, cluster Aurora, transições de estado já corretas) e escrever testes de propriedade capturando esse comportamento.

**Test Cases**:
1. **Seleção por tag**: observar que recursos `ecoFriendly=true` com tag única são parados/iniciados, e escrever teste para garantir que isso se mantém.
2. **Exclusão sem tag**: observar que recursos sem `ecoFriendly=true` são ignorados, e garantir a continuidade.
3. **Read Replica / Aurora**: observar que Read Replicas e clusters Aurora seguem as regras atuais, e garantir que permanecem assim.
4. **Transições de estado RDS/EC2**: observar `available`→stop e `stopped`→start, e garantir que estados intermediários continuam apenas registrados.

### Unit Tests

- Verificação de tags com casing ECS (`key`/`value`) e RDS/EC2 (`Key`/`Value`) pelo helper comum.
- Decisão única de elegibilidade RDS para listas com múltiplas tags (elegível e não elegível).
- Restauração do `desiredCount` original no wakeup de ECS, incluindo fallback quando a tag está ausente.
- Isolamento de erro por recurso (exceção em um recurso não interrompe os demais).
- Leitura resiliente de `REGION` com e sem a env var definida.
- Conversão correta dos horários cron UTC ↔ local de `America/Fortaleza`.

### Property-Based Tests

- Gerar listas de tags aleatórias (chaves/valores e casings variados) e verificar que a elegibilidade é decidida uma única vez e coincide com "existe `ecoFriendly=true` na lista".
- Gerar serviços ECS com `desiredCount` aleatório e verificar que sleep→wakeup restaura o valor original.
- Gerar respostas paginadas de tamanho aleatório e verificar que todos os recursos são processados.
- Gerar entradas não defeituosas e verificar preservação de comportamento entre `F` e `F'`.

### Integration Tests

- Fluxo completo de `sleep_time` seguido da função de wakeup contra um ambiente mockado (moto) cobrindo RDS, ECS e EC2.
- Troca de estados entre ciclos (sleep → wakeup → sleep) preservando o `desiredCount` original dos serviços ECS.
- Validação de que a configuração de agendamento (cron UTC convertida ou Scheduler com timezone) dispara nos horários locais pretendidos e que os nomes de função do README correspondem aos registrados em `serverless.yml`.
