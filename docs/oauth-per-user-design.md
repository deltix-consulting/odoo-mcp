# Ontwerp: OAuth-login met echte per-user rechten

> Status: analyse / beslisdocument — nog geen implementatie.
> Datum: 2026-06-25.
> Context: huidige server is lokaal (stdio), XML-RPC, per gebruiker een eigen Odoo API-key in de OS-keychain.

## 1. Doel

Gebruikers moeten **enkel inloggen**; de rest gebeurt automatisch. Randvoorwaarden uit de afstemming:

- **Prioriteit: echte per-user rechten.** Elke actie draait onder de eigen Odoo-permissies van de gebruiker, auditeerbaar.
- **Odoo 18 en hoger** (mix). Laagste gemene deler = Odoo 18.
- Scope nu: ontwerp + trade-offs, geen code.

## 2. De twee auth-lagen (kern van het probleem)

```
[Claude client] ── laag 1: OAuth ──> [MCP server] ── laag 2: ??? ──> [Odoo]
   wie ben je?                         waarmee praat de server met Odoo?
```

- **Laag 1 (Client ↔ MCP server):** gestandaardiseerd sinds MCP-spec 2025-06-18. Server = OAuth 2.1 resource server, client doet PKCE-flow. Vereist **remote Streamable HTTP** transport (niet de huidige stdio). Dit is het "inloggen" dat de gebruiker ziet.
- **Laag 2 (MCP server ↔ Odoo):** **Odoo 18 XML-RPC accepteert geen OAuth bearer token** — enkel `authenticate(db, user, api_key/password)`. De OAuth-token zegt dus *wie* je bent, maar levert geen Odoo-toegang. Hier zit al het echte werk.

## 3. Waarom het service-account-model afvalt

De bekende OAuth-first referentie ([keboola/odoo-mcp](https://github.com/keboola/odoo-mcp)) valideert de OAuth-JWT, mapt op `hr.employee`, maar praat met Odoo via **één onderliggende service-account API-key**. Gevolg:

- Permissie-plafond = dat van het service-account, niet van de gebruiker.
- Audit trail in Odoo staat op naam van het service-account.
- Per-user scoping gebeurt in *applicatiecode van de MCP-server*, niet door Odoo zelf.

Dat botst frontaal met "echte per-user rechten". **Afgewezen als einddoel.**

Idem voor admin-impersonation (`with_user`/`sudo`): de server houdt dan god-credentials en de DB-laag dwingt de rechten niet af. Afgewezen op governance.

## 4. Gekozen richting — Optie B: OAuth-identiteit + per-user Odoo API-key

```
Eerste keer:
  gebruiker logt in via OAuth (laag 1)
    -> server kent OAuth-identiteit (sub/email, geverifieerd via JWKS)
    -> eenmalige koppeling: gebruiker levert/aanmaakt eigen Odoo API-key
    -> server slaat key versleuteld op, gekoppeld aan OAuth-identiteit

Daarna (elke sessie):
  gebruiker logt enkel in via OAuth
    -> server zoekt de bij die identiteit horende Odoo API-key op
    -> XML-RPC authenticate() onder de EIGEN uid van de gebruiker
    -> Odoo's access rights + record rules dwingen alles af, audit klopt
```

Waarom dit het enige pad is dat aan álle randvoorwaarden voldoet op Odoo 18+:

- ✅ Echte per-user rechten: calls draaien onder de eigen uid → Odoo handhaaft permissies en audit.
- ✅ "Gewoon inloggen": na de eenmalige koppeling is het enkel nog OAuth.
- ✅ Werkt op Odoo 18 (geen native bearer nodig).

Eenmalige kost: de API-key-provisioning per gebruiker (zie §6).

## 5. Wat er moet veranderen t.o.v. vandaag

| Onderdeel | Nu | Nodig voor Optie B |
|-----------|-----|--------------------|
| Transport | stdio (lokaal subprocess) | **Streamable HTTP** (remote, gehost) |
| Auth client↔server | geen (lokaal vertrouwd) | **OAuth 2.1 + PKCE**, token-validatie via JWKS, RFC 9728 resource metadata |
| Credential-opslag | OS-keychain op gebruiker zijn machine | **server-side, versleutelde store** per OAuth-identiteit (KMS/secret manager) |
| Identity provider | n.v.t. | Google/Azure/Odoo-OIDC kiezen (zie §7) |
| Hosting | n.v.t. (lokaal) | EU-hosting, TLS, sessiebeheer, monitoring |
| Multi-instance | `instance` param per call | blijft; key-lookup wordt (identiteit × instance) |

Let op: dit is een wezenlijke architectuurverschuiving van *lokale tool* naar *gehoste multi-tenant dienst*. Het security-zwaartepunt verschuift van "credential op het toestel van de gebruiker" naar "credentials centraal bij ons" — dat vraagt om secret-management, encryptie-at-rest en een helder dataverwerkings-/AVG-verhaal.

## 6. Het lastige stuk: per-user API-key provisioning

Drie manieren om de eenmalige koppeling te doen, oplopend in gemak/complexiteit:

1. **Handmatig plakken (MVP):** gebruiker maakt zelf een API-key in Odoo (Profiel → Accountbeveiliging) en plakt die één keer in een onboarding-scherm. Simpel, geen Odoo-aanpassing. Minste "magie".
2. **Half-geautomatiseerd via Odoo-sessie:** gebruiker logt via OAuth ook in bij Odoo (als Odoo `auth_oauth` met dezelfde IdP gebruikt), server maakt namens hem een API-key via de web-sessie. Vergt dat Odoo dezelfde IdP deelt.
3. **Volledig via Odoo OAuth2-provider (Odoo 19) / JSON-2 bearer:** op Odoo 19-instances kan de eigen bearer-token van de gebruiker rechtstreeks dienen — geen aparte API-key meer. Zie §8.

Aanbevolen MVP: **(1)**, met (2)/(3) als opvolging per instance-versie.

## 7. Keuze identity provider (laag 1)

- **Google/Microsoft (Azure AD):** snelst, breed ondersteund door MCP-clients. Identiteit = e-mail; mapping naar Odoo-user op e-mail.
- **Odoo zelf als OIDC-provider:** mooist conceptueel (één identiteit), maar vergt de betaalde OAuth2-provider-add-on; pas vanaf Odoo 19 native interessant.

Voor "18 en hoger, mix" → start met **Microsoft/Google als IdP**, map op e-mail → Odoo-user.

## 8. Toekomstpad: Odoo 19 JSON-2 bearer API

Odoo 19 introduceert de JSON-2 API met `auth='bearer'`, en XML-RPC/JSON-RPC verdwijnen in Odoo 20 (najaar 2026). Op termijn convergeert laag 2 dus naar échte bearer-tokens per gebruiker — dan vervalt de API-key-store volledig. **Aanbeveling:** bouw laag 2 achter een interface (`OdooAuthStrategy`) met twee implementaties:

- `XmlRpcApiKeyStrategy` (Odoo 18, vandaag)
- `Json2BearerStrategy` (Odoo 19+, toekomst)

Zo is de migratie naar Odoo 19/20 een strategie-swap, geen herbouw.

## 9. Aanbevolen gefaseerde aanpak

1. **Fase 0 — nu:** dit document; beslissen IdP + hosting-locatie.
2. **Fase 1 — remote transport:** stdio → Streamable HTTP, achter TLS. Nog API-key per call (geen OAuth), om de HTTP-server te valideren.
3. **Fase 2 — OAuth laag 1:** PKCE-flow, JWKS-validatie, resource metadata. Gebruiker logt in; key nog handmatig (provisioning optie 1).
4. **Fase 3 — per-user key-store:** versleutelde server-side store, lookup op (identiteit × instance). Hiermee is "gewoon inloggen" rond.
5. **Fase 4 — Odoo 19 strategie:** `Json2BearerStrategy` voor 19+-instances; key-store wordt daar overbodig.

## 10. Open vragen

- Waar hosten (EU)? AVG-verwerkersovereenkomst nodig richting klanten?
- Eén centrale IdP voor alle deltix-klanten, of per klant een eigen tenant?
- Acceptabel dat wij per-user Odoo-credentials centraal bewaren (i.p.v. op het toestel)? Dit is de grootste security-/vertrouwensverschuiving.

## Referenties

- MCP Authorization spec: https://modelcontextprotocol.io/specification/draft/basic/authorization
- keboola/odoo-mcp (OAuth 2.1 + per-user identity referentie): https://github.com/keboola/odoo-mcp
- Odoo External API (XML-RPC/JSON-RPC, 18): https://www.odoo.com/documentation/19.0/developer/reference/external_rpc_api.html
- Odoo 19 JSON-2 / bearer API: https://www.odoo.com/documentation/19.0/developer/reference/external_api.html
