# Deploy - Kanban da Alta Performance Tecnica VIP

## Recomendacao

Hospedar em uma VPS privada ou servidor interno com acesso controlado. Esta ferramenta consulta e pode escrever no IXC, entao nao deve ficar exposta sem HTTPS, firewall, senhas fortes e backup.

## Melhor caminho para producao

1. VPS Linux privada: Hetzner, DigitalOcean, AWS Lightsail, Oracle Cloud ou servidor proprio da VIP.
2. Nginx como proxy reverso com HTTPS.
3. Uvicorn/Gunicorn rodando como servico `systemd`.
4. Firewall liberando somente as portas necessarias.
5. Senhas padrao trocadas antes da primeira publicacao.
6. Token IXC armazenado somente no servidor, fora do pacote publico.
7. Backup dos arquivos `data/` e `logs/` se forem usados como historico operacional.

## Rodar localmente

```powershell
cd projetos\kanban-os-tecnicos-local
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Abra:

```text
http://127.0.0.1:8765
```

## Rodar em servidor Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

## Proxy Nginx sugerido

```nginx
server {
    listen 443 ssl http2;
    server_name kanban.seudominio.com.br;

    ssl_certificate /etc/letsencrypt/live/kanban.seudominio.com.br/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kanban.seudominio.com.br/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Antes de publicar

- Trocar as senhas `adminvip2026` e `supervisorvip2026`.
- Restringir acesso por IP ou VPN, se possivel.
- Criar o arquivo de token IXC no servidor em `.openclaw/secrets/ixc_api_token.txt`, dentro da pasta raiz do projeto implantado.
- Criar backup automatico dos logs de auditoria.
- Testar escrita IXC primeiro em OS controlada.
