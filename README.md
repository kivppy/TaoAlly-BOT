# TaoAlly 🤝

Bot de Discord para gestionar **alianzas entre servidores**, con registro manual, detección automática de invitaciones y un sistema completo de **auto-alianza** validado por requisitos configurables.

## Índice

- [Requisitos previos](#requisitos-previos)
- [Instalación](#instalación)
- [Configuración inicial (staff)](#configuración-inicial-staff)
- [Comandos](#comandos)
- [Cómo funciona la auto-alianza](#cómo-funciona-la-auto-alianza)
- [Detección automática de alianzas manuales](#detección-automática-de-alianzas-manuales)
- [Estructura de datos](#estructura-de-datos)
- [Permisos del bot en Discord](#permisos-del-bot-en-discord)
- [Solución de problemas](#solución-de-problemas)

---

## Requisitos previos

- Python 3.10 o superior
- Una aplicación de Discord creada en el [Portal de Desarrolladores](https://discord.com/developers/applications), con un bot y su **token**
- Los siguientes **Privileged Gateway Intents** activados en el portal:
  - `MESSAGE CONTENT INTENT`
  - `SERVER MEMBERS INTENT`

## Instalación

1. Cloná o descargá este repositorio.
2. Instalá las dependencias:

   ```bash
   pip install discord.py
   ```

3. Configurá el token del bot como variable de entorno:

   ```bash
   export DISCORD_TOKEN="tu_token_aquí"
   ```

   Si no se define `DISCORD_TOKEN`, el bot usa el valor por defecto `PON_TU_TOKEN_AQUI` (no funcional) definido en `bot.py`.

4. Ejecutá el bot:

   ```bash
   python3 bot.py
   ```

Al arrancar, el bot sincroniza automáticamente sus comandos slash con Discord y guarda un log con la fecha/hora de la sincronización. Los datos de cada servidor se guardan en `tao_ally_data.json`, en el mismo directorio.

## Configuración inicial (staff)

Dentro del servidor de Discord, un miembro con permiso de **Administrador** o **Gestionar Servidor** debe correr, en este orden:

1. **`/allychannel`** → elige el canal donde se van a publicar las alianzas.
2. **`/embedconfig`** → personaliza el embed que se publica en cada alianza (título, descripción, color, imagen, footer). Tiene botón de vista previa.
3. *(Opcional, para habilitar auto-alianzas)* **`/autoallysetup`** → configura:
   - La URL de invitación del propio servidor (la "plantilla" que va a circular).
   - El mensaje de MD que reciben los admins de otros servidores al pedir la auto-alianza.
   - Los requisitos mínimos que debe cumplir el servidor solicitante.
   - Activa o desactiva el sistema de auto-alianza.

## Comandos

| Comando | Quién lo usa | Descripción |
|---|---|---|
| `/allychannel` | Staff | Configura el canal de alianzas del servidor. |
| `/embedconfig` | Staff | Configura el embed que se publica en cada alianza, con vista previa. |
| `/allynew servidor:<nombre>` | Staff | Registra una alianza manualmente y publica el embed. |
| `/allylist` | Cualquiera | Muestra el historial de alianzas del servidor, paginado. |
| `/autoallysetup` | Staff | Configura la auto-alianza: URL de invitación propia, mensaje de MD y requisitos para servidores solicitantes. |
| `/autoally` | Admin de otro servidor | Solicita una auto-alianza. Se ejecuta **dentro del servidor 1** (el que ofrece la alianza). |
| `/allyhelp` | Cualquiera | Guía rápida de todos los comandos. Funciona también por mensaje directo. |

Todos los comandos salvo `/allyhelp` solo pueden usarse **dentro de un servidor** (no por MD); si se intentan usar por mensaje directo, el bot responde con un aviso claro en vez de fallar.

## Cómo funciona la auto-alianza

La auto-alianza conecta dos servidores (**server 1**, el que ofrece la alianza y tiene `/autoallysetup` configurado, y **server 2**, el que la solicita) sin necesidad de que el staff del server 1 intervenga manualmente, siempre que se cumplan los requisitos.

1. El **admin del server 2** ejecuta `/autoally` dentro del **server 1**.
2. El bot le manda un **mensaje directo** con un link para agregar el bot a su propio servidor (server 2).
3. El admin agrega el bot al server 2.
4. Al detectar el ingreso, el bot **evalúa automáticamente los requisitos** configurados por el staff del server 1 contra el server 2:
   - Cantidad mínima de miembros.
   - Si se permiten o no canales NSFW.
   - Antigüedad mínima de la cuenta del dueño del servidor.
   - Si se exige insignia de Verificado o Partner de Discord.
   - Palabras prohibidas en el nombre del servidor o de sus canales (para filtrar servidores de hacking, cheats, etc.).
   - Que quien agregó el bot sea realmente admin o dueño de ese servidor.
   - ❌ **Si no cumple**: se le avisa por MD el motivo exacto del rechazo y el bot sale del servidor.
   - ✅ **Si cumple**: continúa al siguiente paso.
5. El bot le pide al admin del server 2, por MD, la **plantilla de su servidor** (descripción + link de invitación).
6. Esa plantilla se publica en el canal de alianzas del **server 1**, marcada como **"🤖 Auto-Alianza"**.
7. **Paso recíproco:** el bot le pide al admin del server 2 que indique en qué **canal de su propio servidor** va a publicar la plantilla del server 1.
8. El admin publica esa plantilla en su servidor y confirma por escrito ("Ya la publiqué, verificar").
9. El bot **busca el link de invitación** del server 1 en ese canal:
   - ✅ Si lo encuentra: la alianza queda confirmada por ambas partes.
   - ⚠️ Si no lo encuentra: arranca un cronómetro de **10 minutos**, con opción de reintentar confirmar o cambiar de canal.
10. Si pasan los 10 minutos sin verificarse, el bot **revierte todo**: borra lo publicado en el server 1 y cancela la alianza, avisando por MD al admin del server 2.

Si en algún punto falla la publicación por falta de permisos del bot en el canal del server 1, el proceso se frena, se revierte el registro y se avisa tanto al admin solicitante como al dueño del server 1.

### Persistencia ante reinicios

Las sesiones de auto-alianza en curso se guardan en el archivo de datos, así que sobreviven a un reinicio del bot (con un límite de 24 horas). Los cronómetros de 10 minutos, al ser de corta duración, no se persisten: si el bot se reinicia en medio de ese paso, se le avisa al admin para que retome el proceso con `/autoally`.

## Detección automática de alianzas manuales

Además de la auto-alianza, el bot detecta automáticamente cuando alguien publica un link de invitación de Discord en el canal de alianzas configurado (por ejemplo, si dos servidores se alían "a mano" y alguien pega el link ahí). En ese caso registra la alianza y publica el embed correspondiente, sin necesidad de usar `/allynew`.

Esta detección se desactiva automáticamente para un mensaje si su autor tiene una auto-alianza en curso con ese mismo servidor, para no interferir con el flujo controlado de `/autoally`.

## Estructura de datos

El bot guarda toda su información en `tao_ally_data.json`, con una entrada por servidor (`guild_id`). Cada servidor incluye, entre otros campos:

- `canal_alianzas`: ID del canal donde se publican las alianzas.
- `embed_config`: título, descripción, color, imagen y footer del embed de alianzas.
- `rol_aviso`: rol que se menciona al publicar una alianza (opcional).
- `alianzas`: historial de alianzas registradas.
- `contador_alianzas`: cantidad total de alianzas del servidor.
- `usuarios`: alianzas registradas por usuario.
- `autoally`: configuración de auto-alianza (activo, URL de invitación, mensaje de MD, requisitos).
- `_autoally_sesiones`: sesiones de auto-alianza en curso (uso interno, para sobrevivir a reinicios).

## Permisos del bot en Discord

Para que el bot funcione correctamente, necesita los siguientes permisos **como mínimo** en el canal de alianzas de cada servidor:

- Ver canal
- Enviar mensajes
- Insertar enlaces (Embed Links)
- Leer historial de mensajes

Y a nivel de servidor, además:

- Intents de **Miembros del servidor** y **Contenido de mensajes** habilitados (tanto en el portal de desarrolladores como en el objeto `intents` del código).

## Solución de problemas

**El bot no publica nada en el canal de alianzas / error `403 Forbidden: Missing Permissions`**
Revisá que el bot tenga los permisos de "Enviar mensajes" e "Insertar enlaces" en ese canal específico (puede haber un permiso denegado a nivel de categoría que sobrescriba el del rol del bot).

**`/autoally` u otro comando falla con `AttributeError: 'NoneType' object has no attribute 'id'`**
Significa que se intentó usar el comando por mensaje directo. Todos los comandos relevantes ya están restringidos a uso dentro de servidores; si esto ocurre, verificá que el bot esté actualizado a la última versión de `bot.py`.

**Error `Invalid Form Body ... Must be between 1 and 45 in length` al abrir un modal**
Los labels de los campos de un modal de Discord no pueden superar los 45 caracteres. Si se modificó algún texto de configuración, revisar que no exceda ese límite.

**Los comandos slash no aparecen actualizados en Discord**
El bot sincroniza los comandos automáticamente al arrancar (una sola vez por proceso). Si acabás de agregar o modificar un comando, reiniciá el bot. Puede tardar hasta una hora en reflejarse globalmente en Discord, aunque en el servidor donde se prueba suele ser casi instantáneo.
