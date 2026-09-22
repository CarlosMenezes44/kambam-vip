# Kanban da Alta Performance Tecnica VIP TELECOM

MVP local para visualizar a bandeja diaria de OS tecnicas do IXC em formato Kanban.

## Funcionalidades

- Login local com perfis `ADM` e `Supervisor`; ADM possui painel gerencial para criar novos logins.
- Aba de auditoria exclusiva para ADM, registrando acessos, dry-runs, escritas e alteracoes de escala.
- Dashboard de produtividade com OS executadas/iniciadas, finalizadas, atrasadas, media por tecnico e OS por turno.
- Alertas popup na tela e notificacoes do navegador quando uma OS e gravada/alocada pelo Kanban.
- Consulta IXC ao vivo por data e filial.
- Botao `Consultar` para controlar quando a busca no IXC roda.
- Kanban por tecnico, com contagem individual.
- Cards com nome do cliente e ID do cliente.
- Busca local por OS, cliente, ID do cliente, assunto ou tecnico.
- Filtros rapidos por status: atrasadas, iniciadas, agendadas e finalizadas.
- Opcao de ocultar OS finalizadas para focar no que ainda precisa de acao.
- Indicador de tempo da consulta no backend e tempo de renderizacao no navegador.
- Modal de detalhe com linha do tempo da OS, nome/ID do cliente e copia de resumo.
- Status do Kanban prioriza o status oficial do IXC: OS `AG` fica como agendada mesmo se o IXC trouxer `data_inicio` preenchida.
- Alerta de agenda atrasada fica separado do status oficial do IXC.
- Arrastar OS para agendamento com escolha de data e hora e gravacao direta no IXC pelo modal.
- Ao arrastar para uma coluna de tecnico, o payload usa o `id_tecnico` da coluna destino.
- Ao clicar em uma OS e usar `Agendar`, o seletor busca tecnicos ativos da filial no IXC, incluindo tecnicos sem OS no dia quando estiverem habilitados para o Kanban.
- Botao `Escala do dia` para o supervisor alimentar a escala local por data/filial, sem gravar nada no IXC.
- Escala local permite status do dia, funcao operacional do dia e dupla/equipe por tecnico.
- Funcoes operacionais da escala: Suporte, Suporte do plantao, Plantao, Retirada e Instalacao.
- Kanban mostra funcao/situacao/dupla embaixo do nome do tecnico; os cards mantem somente o status oficial da OS vindo do IXC.
- Cards mostram `Fibra utilizada` quando a aba Produto da OS no IXC tiver produto com nome/descricao contendo `Fibra` ou `DROP`, somando a `qtde_saida`.
- Registro local de fibra em `data/os-extra.json` permanece como fallback quando nao houver produto Fibra localizado no IXC.
- Botao `Gerar listagem` dentro da escala monta a lista do dia com tecnico, funcao, status e dupla; quando o tecnico estiver de folga, ausente, atestado ou sem escala, a linha sai apenas com nome e status.
- Botao `Relatorio` mostra resumo do dia com OS, atrasos, carga por funcao, OS por dupla, tecnicos em campo sem OS e OS em tecnico fora da escala.
- Escrita real no IXC enviada direto pelo modal de acao do Kanban.
- Checagem automatica a cada 5 minutos, com snapshot local para abertura rapida e alerta de nova OS.

## Stack

- Backend: Python + FastAPI
- Frontend: HTML, CSS e JavaScript puro
- Fonte IXC: `su_oss_chamado`, com enriquecimento por `funcionarios`, `su_oss_assunto` e `su_oss_mov_produto` para fibra utilizada

## Seguranca do MVP

- Escrita protegida por dupla etapa: primeiro valida payload, depois exige confirmacao explicita.
- Token IXC fica no backend, lendo o caminho ja usado pelos scripts: `.openclaw/secrets/ixc_api_token.txt`.
- O frontend nunca recebe o token do IXC.
- O endpoint de reagendamento legado monta e valida payload em dry-run; nao envia alteracao ao IXC:
  - `POST /api/os/{id}/reagendar`
- O endpoint de acoes faz dry-run por padrao e so escreve no IXC com `confirmar_escrita=true` e `confirmacao=ESCREVER_IXC`:
  - `POST /api/os/{id}/acao`
- Cada tentativa de escrita real gera log local em `logs/ixc-actions.jsonl`.
- Endpoint futuro de troca de responsavel existe apenas como bloqueio 403:
  - `POST /api/os/{id}/responsavel`

## Agendamento

O fluxo de arrastar usa a acao **Agendar** do IXC, via endpoint `su_oss_chamado_reagendar`.
Campos-base validados:

- `id_chamado`
- `data_agendamento`
- `data_agendamento_final`
- `status` (`AG`)

Para gravar de verdade, a tela envia a acao direto ao clicar em `Gravar no IXC`.
O backend ainda valida campos obrigatorios, datas, tecnico e janela antes de chamar o IXC.

Se o IXC recusar a operacao por campo obrigatorio especifico, o backend retorna o erro sem expor o token.

## Rodar localmente

No diretorio do projeto:

```powershell
cd projetos\kanban-os-tecnicos-local
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Depois abra:

```text
http://127.0.0.1:8765
```

Credenciais locais iniciais:

```text
ADM: admin / adminvip2026
Supervisor: supervisor / supervisorvip2026
```

Novos logins devem ser criados pelo painel `Gerencial`, visivel apenas para ADM.

O app continua local. Antes de publicar em rede, trocar as senhas padrao e adicionar HTTPS.

## Endpoints

```text
GET /api/health
GET /api/filiais
GET /api/tecnicos?filial=1
GET /api/scale?day=2026-09-10&filial=1
PUT /api/scale
GET /api/os?day=2026-08-27&filial=1
GET /api/os/{id}/cliente
```

Filiais habilitadas neste MVP:

- `1`: Uniao
- `2`: Murici
- `3`: Queimadas
- `4`: Moreno / GEO
- `6`: Belo Jardim / VIP PE
- `7`: Belo Jardim / Netcity Corporativo
- `8`: Uniao Corporativo
- `10`: Santana do Mundau / Totalnet

## Performance

- A consulta usa cache de 30 minutos por data/filial.
- Um snapshot local por data/filial evita nova consulta ao IXC enquanto estiver valido.
- O botao `Atualizar` ignora o cache e consulta o IXC ao vivo.
- Mapas de tecnicos e assuntos ficam em cache por 24 horas.
- Nomes de clientes sao buscados em paralelo depois do filtro da filial e ficam em cache/snapshot.
- O tempo aparece na tela como `consulta X ms / tela Y ms`.

## Proximas fases

1. Adicionar login local simples.
2. Salvar prioridade interna sem alterar IXC.
3. Testar endpoints reais de responsavel em OS controlada.
4. Adicionar perfil de usuario e log de alteracoes antes de qualquer escrita no IXC.
