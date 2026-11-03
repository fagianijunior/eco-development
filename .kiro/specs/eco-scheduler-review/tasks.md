# Implementation Plan

- [x] 1. Escrever teste exploratório da condição de bug (ANTES da correção)
  - **Property 1: Bug Condition** - Correção abrangente do agendador eco
  - **CRÍTICO**: Este teste DEVE FALHAR no código não corrigido - a falha confirma que os bugs existem
  - **NÃO tente corrigir o teste nem o código quando ele falhar** - a falha é o resultado esperado nesta etapa
  - **NOTA**: Este teste codifica o comportamento esperado - ele validará a correção quando passar após a implementação
  - **OBJETIVO**: Surfacear contraexemplos que demonstram cada defeito descrito em `isBugCondition` (design)
  - **Abordagem PBT com escopo**: para os defeitos determinísticos, restringir a propriedade a casos concretos reproduzíveis
  - Configurar mocks do boto3 (`botocore.stub.Stubber` ou `moto`) para ECS, RDS e EC2
  - Caso C_tags_rds (1.2, 1.3): instância RDS com tags `[{Key:"env",Value:"prod"},{Key:"ecoFriendly",Value:"true"}]` - assert de que o recurso é parado SEM emitir "not part of autoshutdown" para as demais tags
  - Caso C_tags_casing (1.4): passar tags no formato RDS (`Key`/`Value`) pela verificação de ECS - expor o `KeyError`/inconsistência
  - Caso C_pagination (1.5): listagem com duas páginas (`NextToken`) para `list_clusters`/`list_services`/`describe_db_instances`/`describe_db_clusters`/`describe_instances` - assert de que todos os recursos das duas páginas são processados
  - Caso C_desiredCount (1.6): serviço ECS com `desiredCount=4` → sleep → wakeup - assert de que volta a `4` (não a `1`)
  - Caso C_errors (1.8): forçar exceção boto3 em um recurso - assert de que os demais recursos ainda são processados
  - Caso C_region (1.11 / edge case): importar/invocar sem a env var `REGION` - assert de que não ocorre `KeyError`
  - Caso C_schedule (1.7): assert de que a expressão cron (UTC) corresponde ao horário local pretendido de `America/Fortaleza` (falha com `0 7`/`0 23`)
  - Caso C_naming (1.1): assert de que todo nome de função citado no README existe em `serverless.yml` (falha com `wakeup_time`)
  - As asserções devem corresponder às Expected Behavior Properties (Property 1) do design
  - Rodar o teste no código NÃO corrigido
  - **RESULTADO ESPERADO**: Teste FALHA (correto - prova que os bugs existem)
  - Documentar os contraexemplos encontrados para entender a causa raiz (ex.: "RDS elegível também classificado como 'not part of autoshutdown'", "serviço ECS religado com desiredCount=1 em vez do original", "recursos além da primeira página ignorados")
  - Marcar a task como concluída quando o teste estiver escrito, executado e a falha documentada
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.11_

- [x] 2. Escrever testes de preservação baseados em propriedades (ANTES da correção)
  - **Property 2: Preservation** - Seleção por tag e transições de estado inalteradas
  - **IMPORTANTE**: Seguir a metodologia observation-first (observar o comportamento no código NÃO corrigido antes de escrever as asserções)
  - Observar, no código NÃO corrigido, o comportamento para entradas que NÃO satisfazem `isBugCondition`:
    - Recurso ECS/RDS/EC2 `ecoFriendly=true` com tag única → selecionado para sleep/wakeup
    - Recurso sem a tag `ecoFriendly=true` → ignorado (nem desligado nem religado)
    - Instância RDS Read Replica ou com Read Replicas associadas → pulada
    - Engine `aurora-mysql`/`aurora-postgresql` → tratada como cluster Aurora (separação `describe_db_instances` vs `describe_db_clusters`)
    - Transições RDS/cluster: `available`→stop no sleep, `stopped`→start no wakeup, `starting`/`stopping` apenas registrados
    - Instância EC2 `ecoFriendly=true` → parada no sleep e iniciada no wakeup
  - Escrever testes de propriedade que gerem entradas não defeituosas (tags únicas, estados variados) e assertem que o comportamento observado se mantém
  - Property-based testing gera muitos casos automaticamente, cobrindo o domínio de entrada para garantias fortes de preservação
  - Rodar os testes no código NÃO corrigido
  - **RESULTADO ESPERADO**: Testes PASSAM (confirma o comportamento base a ser preservado)
  - Marcar a task como concluída quando os testes estiverem escritos, executados e passando no código não corrigido
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 3. Correção abrangente do agendador eco (handler.py, serverless.yml, README.md)

  - [x] 3.1 Implementar leitura resiliente de REGION
    - Substituir `region = os.environ['REGION']` por `os.environ.get("REGION") or os.environ.get("AWS_REGION")`
    - Evitar leitura rígida em escopo de módulo; resolver a região dentro das funções ou de forma tolerante a `None`, recorrendo à região default do runtime
    - _Bug_Condition: isBugCondition(input) onde NOT envDefined("REGION") AND readsRegionAsRequired()_
    - _Expected_Behavior: expectedBehavior(result) item (i) - recorre à região default do runtime_
    - _Preservation: Preservation Requirements do design (comportamento de seleção/transição inalterado)_
    - _Requirements: 2.11_

  - [x] 3.2 Substituir print por logging estruturado
    - Configurar `logger = logging.getLogger()` com `logger.setLevel(logging.INFO)` no topo do módulo
    - Substituir todos os `print(...)` por `logger.info/warning/error(...)` com contexto (identificador do recurso, operação, estado)
    - _Bug_Condition: isBugCondition(input) onde input.emitsLog AND usesPrint()_
    - _Expected_Behavior: expectedBehavior(result) item (g) - logs estruturados com níveis de severidade_
    - _Preservation: Preservation Requirements do design_
    - _Requirements: 2.9_

  - [x] 3.3 Introduzir helper comum de verificação de tags
    - Criar `tag_matches(tags, wanted_key, wanted_value)` que normaliza o casing: `key = tag.get("Key", tag.get("key"))`, `value = tag.get("Value", tag.get("value"))`
    - Usá-lo em ECS, RDS e EC2 no lugar de `filter_by_tags` e das comparações inline `tag['Key']==...`
    - _Bug_Condition: isBugCondition(input) onde input.evaluatesTags AND NOT usesCommonTagHelper()_
    - _Expected_Behavior: expectedBehavior(result) item (b) - helper comum que trata o casing de cada serviço_
    - _Preservation: Preservation Requirements 3.1, 3.2 (seleção por tag preservada)_
    - _Requirements: 2.4_

  - [x] 3.4 Corrigir a lógica de filtro de tags do RDS
    - Decidir a elegibilidade uma única vez por recurso: `eligible = tag_matches(tagList, "ecoFriendly", "true")`
    - Remover os ramos `elif tag[...] != ... and tag[...] != ...` que avaliam tag a tag
    - Classificar como "fora do autoshutdown" apenas quando `eligible` é falso, após inspecionar a lista inteira
    - Aplicar o mesmo tratamento a `describe_db_instances` e `describe_db_clusters`, tanto no sleep quanto no wakeup
    - _Bug_Condition: isBugCondition(input) onde input.rdsResource.tags CONTAINS ecoFriendly=true AND countTags > 1_
    - _Expected_Behavior: expectedBehavior(result) item (a) - decidir elegibilidade uma única vez após inspecionar a lista inteira_
    - _Preservation: Preservation Requirements 3.1, 3.2, 3.3, 3.4, 3.5_
    - _Requirements: 2.2, 2.3_

  - [x] 3.5 Adicionar paginação às listagens AWS
    - Usar `get_paginator` para `list_clusters`, `list_services`, `describe_db_instances`, `describe_db_clusters` e `describe_instances`
    - Iterar todas as páginas e processar todos os itens
    - _Bug_Condition: isBugCondition(input) onde listCallIsPaginated(input.listCall) AND NOT usesPaginator(input.listCall)_
    - _Expected_Behavior: expectedBehavior(result) item (c) - percorrer todas as páginas e processar todos os recursos_
    - _Preservation: Preservation Requirements 3.7 (cobertura de RDS, ECS e EC2 em cada ciclo)_
    - _Requirements: 2.5_

  - [x] 3.6 Restaurar o desiredCount original dos serviços ECS
    - No sleep, antes de definir `desiredCount=0`, ler o `desiredCount` atual via `describe_services` e persistir o original como tag no serviço (ex.: `ecoOriginalDesiredCount`) via `tag_resource`
    - No wakeup, ler a tag e restaurar o valor; na ausência da tag, aplicar fallback seguro documentado
    - _Bug_Condition: isBugCondition(input) onde operation == WAKEUP_ECS AND originalDesiredCount != 1 AND wakeupSetsFixedDesiredCount(1)_
    - _Expected_Behavior: expectedBehavior(result) item (d) - restaurar a contagem desejada original anterior ao desligamento_
    - _Preservation: Preservation Requirements 3.1, 3.7_
    - _Requirements: 2.6_

  - [x] 3.7 Isolar falhas por recurso com tratamento de erros
    - Envolver cada operação por recurso (cada serviço ECS, cada instância/cluster RDS, cada instância EC2) em `try/except`
    - Registrar o erro via `logger.error(...)` e seguir para o próximo recurso, sem propagar a exceção para fora do loop
    - _Bug_Condition: isBugCondition(input) onde boto3CallRaises(input.resource) AND NOT isolatedPerResource(input.resource)_
    - _Expected_Behavior: expectedBehavior(result) item (f) - tratar falhas boto3 por recurso, registrando e continuando_
    - _Preservation: Preservation Requirements 3.7_
    - _Requirements: 2.8_

  - [x] 3.8 Ajustar o agendamento cron/timezone no serverless.yml
    - Converter os horários locais de `America/Fortaleza` (UTC-3) para UTC: `cron(0 7 ...)` → `cron(0 10 ...)` e `cron(0 23 ...)` → `cron(0 2 ...)` no dia seguinte (atenção ao deslocamento de dia da semana) — OU migrar para EventBridge Scheduler com `timezone: America/Fortaleza`
    - Documentar a opção escolhida para que o horário efetivo corresponda ao documentado (Fortaleza não observa horário de verão, offset fixo)
    - _Bug_Condition: isBugCondition(input) onde effectiveTriggerTimeUTC(schedule) != intendedLocalTime(schedule, "America/Fortaleza")_
    - _Expected_Behavior: expectedBehavior(result) item (e) - disparar no horário local pretendido para America/Fortaleza_
    - _Preservation: Preservation Requirements 3.7_
    - _Requirements: 2.7_

  - [x] 3.9 Alinhar a política IAM ao uso real
    - Remover `ecs:StartTask` e `ecs:StopTask` do bloco de permissões
    - Manter `ecs:UpdateService`, `ecs:ListClusters`, `ecs:DescribeClusters`, `ecs:ListServices`; adicionar `ecs:DescribeServices`, `ecs:TagResource` e `ecs:ListTagsForResource` se a restauração via tag (3.6) for adotada
    - _Bug_Condition: isBugCondition(input) onde input.iamPolicy GRANTS ("ecs:StartTask" OR "ecs:StopTask")_
    - _Expected_Behavior: expectedBehavior(result) item (h) - política IAM sem ecs:StartTask/ecs:StopTask_
    - _Preservation: Preservation Requirements 3.1, 3.6, 3.7_
    - _Requirements: 2.10_

  - [x] 3.10 Unificar nomes de função entre serverless.yml, handler.py e README.md
    - Definir o nome canônico da função de wakeup (manter `adventure_time` ou renomear para `wakeup_time`) e refletir a mesma escolha nos três arquivos
    - Corrigir os comandos de invocação do README para os nomes realmente registrados; descrever a estratégia de agendamento (UTC convertido vs Scheduler com timezone) e a tag de contagem desejada, se aplicável
    - _Bug_Condition: isBugCondition(input) onde input.documentedFunctionName NOT IN registeredFunctions(serverless.yml)_
    - _Expected_Behavior: expectedBehavior(result) item (j) - nomes de função consistentes entre serverless.yml, handler.py e README.md_
    - _Preservation: Preservation Requirements 3.7_
    - _Requirements: 2.1_

  - [x] 3.11 Verificar que o teste exploratório da condição de bug agora passa
    - **Property 1: Expected Behavior** - Correção abrangente do agendador eco
    - **IMPORTANTE**: Re-executar o MESMO teste da task 1 - NÃO escrever um novo teste
    - O teste da task 1 codifica o comportamento esperado; ao passar, confirma que o comportamento esperado foi satisfeito
    - Rodar o teste exploratório da condição de bug da task 1
    - **RESULTADO ESPERADO**: Teste PASSA (confirma que os bugs foram corrigidos)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11_

  - [x] 3.12 Verificar que os testes de preservação continuam passando
    - **Property 2: Preservation** - Seleção por tag e transições de estado inalteradas
    - **IMPORTANTE**: Re-executar os MESMOS testes da task 2 - NÃO escrever novos testes
    - Rodar os testes de preservação baseados em propriedades da task 2
    - **RESULTADO ESPERADO**: Testes PASSAM (confirma que não há regressões)
    - Confirmar que todos os testes continuam passando após a correção (sem regressões na seleção por tag, nas transições de estado RDS/EC2/ECS e nas regras de Read Replica/Aurora)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 4. Checkpoint - Garantir que todos os testes passam
  - Executar a suíte completa (exploratório/fix, preservação, unitários, baseados em propriedades e integração com moto)
  - Garantir que todos os testes passam; perguntar ao usuário caso surjam dúvidas
