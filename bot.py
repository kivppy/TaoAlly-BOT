import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import re
import math
import datetime
import asyncio

# =============================================================================
# CONFIG BASICA
# =============================================================================
DATA_FILE = "tao_ally_data.json"
TOKEN = os.getenv("DISCORD_TOKEN", "PON_TU_TOKEN_AQUI")

BOT_NAME = "TaoAlly"

INVITE_REGEX = re.compile(r"(?:discord\.gg/|discord\.com/invite/)([a-zA-Z0-9-]+)", re.IGNORECASE)


# =============================================================================
# ALMACENAMIENTO (JSON simple, un archivo para todos los servidores)
# =============================================================================
def cargar_datos():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            contenido = json.load(f)
        return contenido if isinstance(contenido, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def guardar_datos(data):
    tmp_file = DATA_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp_file, DATA_FILE)
    except OSError as e:
        print(f"Error al guardar {DATA_FILE}: {e}")


data = cargar_datos()

# =============================================================================
# AUTO-ALIANZA: estado en memoria de solicitudes activas
# Estructura: { admin_solicitante_id: { "guild_origen_id": int, "creado": datetime } }
# Se guarda también una copia liviana en el JSON (clave "_autoally_sesiones") para
# sobrevivir a un reinicio del bot; las tareas de cronómetro (10 min) en cambio no
# se pueden persistir y se pierden si el bot se apaga mientras corren.
# =============================================================================
autoally_sesiones = {}
AUTOALLY_SESION_TTL_HORAS = 24

# Tareas asyncio de timeout (10 min) para la verificación de publicación recíproca
# Estructura: { admin_id: asyncio.Task }
tareas_verificacion = {}


def guardar_sesiones_autoally():
    data["_autoally_sesiones"] = {
        str(uid): {
            "guild_origen_id": s["guild_origen_id"],
            "creado_iso": s["creado"].isoformat(),
        }
        for uid, s in autoally_sesiones.items()
    }
    guardar_datos(data)


def cargar_sesiones_autoally():
    crudas = data.get("_autoally_sesiones", {})
    if not isinstance(crudas, dict):
        return
    ahora = discord.utils.utcnow()
    for uid_str, s in crudas.items():
        try:
            uid = int(uid_str)
            creado = datetime.datetime.fromisoformat(s["creado_iso"])
            if creado.tzinfo is None:
                creado = creado.replace(tzinfo=datetime.timezone.utc)
            if (ahora - creado).total_seconds() > AUTOALLY_SESION_TTL_HORAS * 3600:
                continue  # ya expirada, no la restauramos
            autoally_sesiones[uid] = {"guild_origen_id": s["guild_origen_id"], "creado": creado}
        except (KeyError, ValueError, TypeError):
            continue


def get_guild_data(guild_id: int) -> dict:
    gid = str(guild_id)
    if gid not in data or not isinstance(data[gid], dict):
        data[gid] = {}
    g = data[gid]

    g.setdefault("canal_alianzas", None)
    g.setdefault("rol_aviso", None)
    g.setdefault("deteccion_automatica", True)
    g.setdefault("contador_alianzas", 0)
    g.setdefault("contador_id", 0)
    g.setdefault("usuarios", {})
    g.setdefault("alianzas", [])
    g.setdefault("embed_config", {
        "titulo": "🤝 Nueva alianza completada",
        "descripcion": "**{username}** completó una alianza con **{servername}**.",
        "color": "0x2b2d31",
        "imagen": None,
        "footer": None,
    })
    g["embed_config"].setdefault("titulo", "🤝 Nueva alianza completada")
    g["embed_config"].setdefault("descripcion", "**{username}** completó una alianza con **{servername}**.")
    g["embed_config"].setdefault("color", "0x2b2d31")
    g["embed_config"].setdefault("imagen", None)
    g["embed_config"].setdefault("footer", None)

    # --- Auto-alianza ---
    g.setdefault("autoally", {
        "activo": False,
        "invite_url": None,  # URL de invitación/plantilla de ESTE servidor (server 1), para que el server 2 la publique
        "mensaje_dm": (
            "¡Hola! Gracias por tu interés en aliarte con **{servername}** 🤝\n\n"
            "Para continuar, agregá el bot a **tu servidor** usando este enlace:\n{bot_invite_url}\n\n"
            "Cuando lo agregues, el bot va a detectar tu servidor automáticamente y, si cumple los "
            "requisitos, te va a pedir que publiques la plantilla de {servername} en un canal tuyo "
            "para completar la alianza."
        ),
        "requisitos": {
            "miembros_minimos": 0,
            "permitir_nsfw": True,
            "antiguedad_cuenta_dueno_dias": 0,
            "palabras_prohibidas": [],  # nombres/temas prohibidos en el nombre del server (hacking, cheats, etc.)
            "servidor_verificado_o_partner": False,
        },
    })
    g["autoally"].setdefault("activo", False)
    g["autoally"].setdefault("invite_url", None)
    g["autoally"].setdefault("mensaje_dm", (
        "¡Hola! Gracias por tu interés en aliarte con **{servername}** 🤝\n\n"
        "Para continuar, agregá el bot a **tu servidor** usando este enlace:\n{bot_invite_url}\n\n"
        "Cuando lo agregues, el bot va a detectar tu servidor automáticamente y, si cumple los "
        "requisitos, te va a pedir que publiques la plantilla de {servername} en un canal tuyo "
        "para completar la alianza."
    ))
    g["autoally"].setdefault("requisitos", {})
    g["autoally"]["requisitos"].setdefault("miembros_minimos", 0)
    g["autoally"]["requisitos"].setdefault("permitir_nsfw", True)
    g["autoally"]["requisitos"].setdefault("antiguedad_cuenta_dueno_dias", 0)
    g["autoally"]["requisitos"].setdefault("palabras_prohibidas", [])
    g["autoally"]["requisitos"].setdefault("servidor_verificado_o_partner", False)

    # Solicitudes de auto-alianza pendientes: {solicitante_id: {"guild_id_origen": ..., "creado_iso": ...}}
    g.setdefault("autoally_pendientes", {})

    return g


# =============================================================================
# PERMISOS: ADMIN o STAFF (Administrador / Gestionar Servidor / dueño)
# =============================================================================
def es_staff(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild.owner_id == member.id:
        return True
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if es_staff(interaction):
            return True
        raise app_commands.CheckFailure("Solo el staff o administradores pueden usar este comando.")
    return app_commands.check(predicate)


async def manejar_error_staff(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.NoPrivateMessage):
        msg = "🚫 Este comando solo se puede usar dentro de un servidor, no por mensaje directo."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "🚫 Solo el staff (permiso de Administrador o Gestionar Servidor) puede usar este comando."
    else:
        print(f"Error en comando: {error}")
        msg = f"Ocurrió un error: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# =============================================================================
# LOGICA DE ALIANZAS
# =============================================================================
def reemplazar_placeholders(texto: str, contexto: dict) -> str:
    if not texto:
        return texto
    for clave, valor in contexto.items():
        texto = texto.replace(clave, str(valor))
    return texto


def registrar_alianza(guild: discord.Guild, usuario: discord.abc.User, servidor_aliado: str):
    gdata = get_guild_data(guild.id)
    gdata["contador_alianzas"] += 1
    gdata["contador_id"] += 1

    uid = str(usuario.id)
    stats_usuario = gdata["usuarios"].setdefault(uid, {"alianzas": 0})
    stats_usuario["alianzas"] += 1

    ahora = discord.utils.utcnow()
    entrada = {
        "id": gdata["contador_id"],
        "usuario_id": usuario.id,
        "servidor_aliado": servidor_aliado,
        "fecha_iso": ahora.isoformat(),
        "alianzas_usuario": stats_usuario["alianzas"],
        "alianzas_server": gdata["contador_alianzas"],
    }
    gdata["alianzas"].append(entrada)
    guardar_datos(data)
    return gdata, entrada


def construir_color(valor_hex: str) -> discord.Color:
    try:
        return discord.Color(int(valor_hex, 16))
    except (ValueError, TypeError):
        return discord.Color(0x2b2d31)


def construir_embed_alianza(guild: discord.Guild, gdata: dict, entrada: dict, usuario_nombre: str, servidor_aliado: str, es_auto: bool = False, plantilla_embed: discord.Embed = None) -> discord.Embed:
    cfg = gdata["embed_config"]
    contexto = {
        "{username}": usuario_nombre,
        "{servername}": servidor_aliado,
    }
    titulo = reemplazar_placeholders(cfg.get("titulo"), contexto)
    descripcion = reemplazar_placeholders(cfg.get("descripcion"), contexto)
    footer_texto = reemplazar_placeholders(cfg.get("footer"), contexto)

    if es_auto:
        titulo = f"🤖 [Auto-Alianza] {titulo}" if titulo else "🤖 Auto-Alianza completada"

    ahora = datetime.datetime.fromisoformat(entrada["fecha_iso"])

    embed = discord.Embed(title=titulo or None, description=descripcion or None, color=construir_color(cfg.get("color", "0x2b2d31")))
    embed.add_field(name="🤝 Alianza completada por", value=usuario_nombre, inline=False)
    embed.add_field(name="🏠 Servidor", value=guild.name, inline=True)
    embed.add_field(name="🌐 Aliados con", value=servidor_aliado, inline=True)
    embed.add_field(name="📊 Alianzas del usuario", value=str(entrada["alianzas_usuario"]), inline=True)
    embed.add_field(name="📅 Fecha", value=ahora.strftime("%d/%m/%Y"), inline=True)
    embed.add_field(name="🕒 Hora", value=ahora.strftime("%H:%M UTC"), inline=True)
    embed.add_field(name="🏆 Alianzas del servidor", value=str(entrada["alianzas_server"]), inline=True)
    if es_auto:
        embed.add_field(name="🤖 Tipo de alianza", value="Automática (auto-alianza)", inline=True)

    if plantilla_embed is not None:
        if plantilla_embed.image:
            embed.set_image(url=plantilla_embed.image.url)
        elif cfg.get("imagen"):
            embed.set_image(url=cfg["imagen"])
        if plantilla_embed.thumbnail:
            embed.set_thumbnail(url=plantilla_embed.thumbnail.url)
    elif cfg.get("imagen"):
        embed.set_image(url=cfg["imagen"])

    if footer_texto:
        embed.set_footer(text=footer_texto)

    return embed


async def enviar_alerta_alianza(canal: discord.abc.Messageable, guild: discord.Guild, gdata: dict, entrada: dict, usuario_nombre: str, servidor_aliado: str, es_auto: bool = False, plantilla_embed: discord.Embed = None, plantilla_contenido: str = None):
    embed = construir_embed_alianza(guild, gdata, entrada, usuario_nombre, servidor_aliado, es_auto=es_auto, plantilla_embed=plantilla_embed)
    contenido = None
    rol_id = gdata.get("rol_aviso")
    if rol_id:
        rol = guild.get_role(rol_id)
        if rol:
            contenido = rol.mention
    mensajes_enviados = []

    msg = await canal.send(content=contenido, embed=embed)
    mensajes_enviados.append(msg)

    # Si el server 2 mandó su propia plantilla (embed o texto), la publicamos también.
    # Esto va en su propio try/except para que un fallo acá no quede en silencio ni
    # se confunda con un fallo del embed principal (que ya se mandó bien arriba).
    if plantilla_embed is not None:
        try:
            msg2 = await canal.send(embed=plantilla_embed)
            mensajes_enviados.append(msg2)
        except discord.HTTPException as e:
            print(f"[enviar_alerta_alianza] No se pudo publicar el embed de la plantilla en {canal}: {e}")
    elif plantilla_contenido:
        try:
            msg2 = await canal.send(content=plantilla_contenido[:2000])
            mensajes_enviados.append(msg2)
        except discord.HTTPException as e:
            print(f"[enviar_alerta_alianza] No se pudo publicar el contenido de la plantilla en {canal}: {e}")

    return mensajes_enviados


async def detectar_servidor_aliado(bot: commands.Bot, message: discord.Message):
    """Intenta reconocer el nombre del servidor aliado a partir de una
    plantilla que otro server mandó al canal (embed propio o un link de
    invitación)."""
    if message.embeds:
        emb = message.embeds[0]
        if emb.author and emb.author.name:
            return emb.author.name.strip()[:100]
        if emb.title:
            return emb.title.strip()[:100]
        if emb.description:
            primera_linea = emb.description.strip().splitlines()[0].strip()
            if primera_linea:
                return primera_linea[:100]

    match = INVITE_REGEX.search(message.content or "")
    if match:
        codigo = match.group(1)
        try:
            invite = await bot.fetch_invite(codigo)
        except (discord.NotFound, discord.HTTPException):
            return None
        if invite.guild:
            return invite.guild.name

    return None


# =============================================================================
# AUTO-ALIANZA: EVALUACION DE REQUISITOS
# =============================================================================
def evaluar_requisitos_autoally(guild_candidato: discord.Guild, requisitos: dict) -> list:
    """Devuelve una lista de strings con los motivos de rechazo.
    Lista vacía = cumple todos los requisitos."""
    motivos = []

    miembros_min = requisitos.get("miembros_minimos", 0)
    if miembros_min and (guild_candidato.member_count or 0) < miembros_min:
        motivos.append(
            f"El servidor tiene {guild_candidato.member_count or 0} miembros, "
            f"se requiere un mínimo de {miembros_min}."
        )

    if not requisitos.get("permitir_nsfw", True):
        canales_nsfw = [c for c in guild_candidato.text_channels if c.is_nsfw()]
        if canales_nsfw:
            motivos.append("El servidor tiene canales marcados como NSFW, y no están permitidos.")

    antiguedad_min = requisitos.get("antiguedad_cuenta_dueno_dias", 0)
    if antiguedad_min:
        dueno = guild_candidato.owner
        if dueno is None:
            try:
                dueno = None  # se resuelve afuera con fetch si hace falta
            except Exception:
                dueno = None
        if dueno is not None:
            edad_dias = (discord.utils.utcnow() - dueno.created_at).days
            if edad_dias < antiguedad_min:
                motivos.append(
                    f"La cuenta del dueño del servidor tiene {edad_dias} días, "
                    f"se requiere una antigüedad mínima de {antiguedad_min} días."
                )

    if requisitos.get("servidor_verificado_o_partner", False):
        flags = getattr(guild_candidato, "features", [])
        if "VERIFIED" not in flags and "PARTNERED" not in flags:
            motivos.append("El servidor no cuenta con insignia de Verificado o Partner de Discord, y es requisito.")

    palabras_prohibidas = requisitos.get("palabras_prohibidas", [])
    if palabras_prohibidas:
        nombre_lower = guild_candidato.name.lower()
        canales_texto = " ".join(c.name.lower() for c in guild_candidato.channels)
        for palabra in palabras_prohibidas:
            p = palabra.strip().lower()
            if not p:
                continue
            if p in nombre_lower or p in canales_texto:
                motivos.append(f"Se detectó contenido/palabra no permitida relacionada a: «{palabra.strip()}».")
                break  # con una alcanza para el motivo, no hace falta listarlas todas

    return motivos


def limpiar_sesiones_expiradas():
    ahora = discord.utils.utcnow()
    expiradas = [
        uid for uid, s in autoally_sesiones.items()
        if (ahora - s["creado"]).total_seconds() > AUTOALLY_SESION_TTL_HORAS * 3600
    ]
    for uid in expiradas:
        autoally_sesiones.pop(uid, None)
    if expiradas:
        guardar_sesiones_autoally()


def resolver_canal_por_texto(guild: discord.Guild, texto: str):
    """Acepta un ID de canal, una mención <#id>, o un link de mensaje/canal de Discord."""
    texto = texto.strip()

    match_id = re.search(r"(\d{15,25})", texto)
    if not match_id:
        return None
    canal_id = int(match_id.group(1))

    # Si el texto es un link de mensaje (.../channels/guild_id/channel_id/message_id),
    # puede haber más de un número; el ID de canal es el segundo grupo numérico.
    todos_ids = re.findall(r"(\d{15,25})", texto)
    if "/channels/" in texto and len(todos_ids) >= 2:
        canal_id = int(todos_ids[1])

    canal = guild.get_channel(canal_id)
    return canal


async def buscar_invite_en_canal(canal: discord.TextChannel, invite_url_configurada: str, limite_mensajes: int = 50):
    """Busca en los últimos mensajes del canal un link que coincida con la URL de
    invitación configurada por el server 1 (compara por código de invitación)."""
    match_config = INVITE_REGEX.search(invite_url_configurada or "")
    codigo_config = match_config.group(1).lower() if match_config else None
    if not codigo_config:
        return False

    try:
        async for msg in canal.history(limit=limite_mensajes):
            contenido = msg.content or ""
            for embed in msg.embeds:
                contenido += " " + (embed.description or "") + " " + " ".join(
                    f.value for f in embed.fields if f.value
                )
            for match in INVITE_REGEX.finditer(contenido):
                if match.group(1).lower() == codigo_config:
                    return True
    except discord.Forbidden:
        return False
    return False


# =============================================================================
# BOT
# =============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.ya_sincronizo = False  # evita re-sincronizar comandos en cada reconexión (on_ready puede dispararse varias veces)


# =============================================================================
# UI: /allychannel
# =============================================================================
def texto_estado_canal(guild: discord.Guild, gdata: dict) -> str:
    canal_id = gdata.get("canal_alianzas")
    canal = guild.get_channel(canal_id) if canal_id else None
    estado_deteccion = "✅ Activada" if gdata.get("deteccion_automatica", True) else "❌ Desactivada"
    return (
        f"**Canal de alertas actual:** {canal.mention if canal else 'Sin configurar'}\n"
        f"**Detección automática de plantillas:** {estado_deteccion}\n\n"
        "Elegí el canal en el menú de abajo, o activá/desactivá la detección automática."
    )


class AllyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(placeholder="Elegí el canal donde se envían las alianzas", channel_types=[discord.ChannelType.text])

    async def callback(self, interaction: discord.Interaction):
        gdata = get_guild_data(self.guild_id)
        gdata["canal_alianzas"] = self.values[0].id
        guardar_datos(data)
        await interaction.response.edit_message(content=texto_estado_canal(interaction.guild, gdata), view=self.view)


class ToggleDeteccionButton(discord.ui.Button):
    def __init__(self, guild_id: int):
        gdata = get_guild_data(guild_id)
        activo = gdata.get("deteccion_automatica", True)
        super().__init__(
            label="Desactivar detección automática" if activo else "Activar detección automática",
            style=discord.ButtonStyle.grey,
            emoji="🔁",
            row=1,
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        gdata = get_guild_data(self.guild_id)
        gdata["deteccion_automatica"] = not gdata.get("deteccion_automatica", True)
        guardar_datos(data)
        self.label = "Desactivar detección automática" if gdata["deteccion_automatica"] else "Activar detección automática"
        await interaction.response.edit_message(content=texto_estado_canal(interaction.guild, gdata), view=self.view)


class AllyChannelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.add_item(AllyChannelSelect(guild_id))
        self.add_item(ToggleDeteccionButton(guild_id))


# =============================================================================
# UI: /embedconfig
# =============================================================================
class EmbedTextModal(discord.ui.Modal, title=f"Configurar embed de {BOT_NAME}"):
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        cfg = get_guild_data(guild_id)["embed_config"]

        self.campo_titulo = discord.ui.TextInput(
            label="Título (soporta {username} {servername})",
            default=cfg.get("titulo") or "",
            max_length=256,
            required=False,
        )
        self.campo_descripcion = discord.ui.TextInput(
            label="Descripción (soporta {username} {servername})",
            style=discord.TextStyle.paragraph,
            default=cfg.get("descripcion") or "",
            max_length=2000,
            required=False,
        )
        self.campo_color = discord.ui.TextInput(
            label="Color (hex, ej: 0x2b2d31)",
            default=cfg.get("color") or "0x2b2d31",
            max_length=10,
            required=False,
        )
        self.campo_imagen = discord.ui.TextInput(
            label="URL de imagen (opcional)",
            default=cfg.get("imagen") or "",
            max_length=500,
            required=False,
        )
        self.campo_footer = discord.ui.TextInput(
            label="Footer (soporta {username} {servername})",
            default=cfg.get("footer") or "",
            max_length=200,
            required=False,
        )
        for campo in (self.campo_titulo, self.campo_descripcion, self.campo_color, self.campo_imagen, self.campo_footer):
            self.add_item(campo)

    async def on_submit(self, interaction: discord.Interaction):
        gdata = get_guild_data(self.guild_id)
        gdata["embed_config"]["titulo"] = self.campo_titulo.value or "🤝 Nueva alianza completada"
        gdata["embed_config"]["descripcion"] = self.campo_descripcion.value or ""
        gdata["embed_config"]["color"] = self.campo_color.value or "0x2b2d31"
        gdata["embed_config"]["imagen"] = self.campo_imagen.value or None
        gdata["embed_config"]["footer"] = self.campo_footer.value or None
        guardar_datos(data)
        await interaction.response.send_message("✅ Embed actualizado.", ephemeral=True)


class RolAvisoSelect(discord.ui.RoleSelect):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(placeholder="Elegí el rol a mencionar en cada alianza")

    async def callback(self, interaction: discord.Interaction):
        gdata = get_guild_data(self.guild_id)
        gdata["rol_aviso"] = self.values[0].id
        guardar_datos(data)
        await interaction.response.edit_message(content=f"Rol de aviso configurado: {self.values[0].mention}", view=None)


class RolAvisoView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.add_item(RolAvisoSelect(guild_id))


class EmbedConfigView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label="Editar texto/color/imagen", style=discord.ButtonStyle.blurple, emoji="✏️", row=0)
    async def editar_texto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmbedTextModal(self.guild_id))

    @discord.ui.button(label="Rol a mencionar", style=discord.ButtonStyle.grey, emoji="🔔", row=0)
    async def rol_mencion(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Elegí el rol a mencionar en cada alianza:", view=RolAvisoView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Quitar rol", style=discord.ButtonStyle.red, emoji="🔕", row=0)
    async def quitar_rol(self, interaction: discord.Interaction, button: discord.ui.Button):
        gdata = get_guild_data(self.guild_id)
        gdata["rol_aviso"] = None
        guardar_datos(data)
        await interaction.response.send_message("Rol de aviso desactivado.", ephemeral=True)

    @discord.ui.button(label="Vista previa", style=discord.ButtonStyle.green, emoji="👁️", row=1)
    async def vista_previa(self, interaction: discord.Interaction, button: discord.ui.Button):
        gdata = get_guild_data(self.guild_id)
        entrada_falsa = {
            "id": 0,
            "fecha_iso": discord.utils.utcnow().isoformat(),
            "alianzas_usuario": gdata["usuarios"].get(str(interaction.user.id), {}).get("alianzas", 0) + 1,
            "alianzas_server": gdata["contador_alianzas"] + 1,
        }
        embed = construir_embed_alianza(interaction.guild, gdata, entrada_falsa, interaction.user.mention, "Servidor de Ejemplo")
        await interaction.response.send_message("Vista previa (con datos de ejemplo):", embed=embed, ephemeral=True)


# =============================================================================
# UI: /autoallysetup
# =============================================================================
def texto_estado_autoally(gdata: dict) -> str:
    aa = gdata["autoally"]
    req = aa["requisitos"]
    estado = "✅ Activada" if aa.get("activo") else "❌ Desactivada"
    url = aa.get("invite_url") or "Sin configurar"
    nsfw = "Permitido" if req.get("permitir_nsfw", True) else "🚫 No permitido"
    palabras = ", ".join(req.get("palabras_prohibidas", [])) or "Ninguna"
    return (
        f"**Auto-alianza:** {estado}\n"
        f"**URL de invitación (plantilla de este server):** {url}\n\n"
        f"**Requisitos actuales:**\n"
        f"👥 Miembros mínimos: `{req.get('miembros_minimos', 0)}`\n"
        f"🔞 Canales NSFW: {nsfw}\n"
        f"📅 Antigüedad mínima cuenta del dueño: `{req.get('antiguedad_cuenta_dueno_dias', 0)} días`\n"
        f"🏅 Requiere Verificado/Partner: `{'Sí' if req.get('servidor_verificado_o_partner') else 'No'}`\n"
        f"🚫 Palabras prohibidas (nombre/canales): {palabras}\n\n"
        "Usá los botones para configurar cada parte."
    )


class AutoAllyInviteModal(discord.ui.Modal, title="Configurar URL de invitación"):
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        aa = get_guild_data(guild_id)["autoally"]
        self.campo_url = discord.ui.TextInput(
            label="URL de invitación de este servidor",
            placeholder="https://discord.gg/tuinvite",
            default=aa.get("invite_url") or "",
            max_length=200,
            required=True,
        )
        self.add_item(self.campo_url)

    async def on_submit(self, interaction: discord.Interaction):
        url = self.campo_url.value.strip()
        if not INVITE_REGEX.search(url):
            await interaction.response.send_message("⚠️ Esa no parece una URL de invitación válida de Discord.", ephemeral=True)
            return
        gdata = get_guild_data(self.guild_id)
        gdata["autoally"]["invite_url"] = url
        guardar_datos(data)
        await interaction.response.send_message(f"✅ URL de invitación configurada: {url}", ephemeral=True)


class AutoAllyMensajeModal(discord.ui.Modal, title="Mensaje pre-configurado (MD)"):
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        aa = get_guild_data(guild_id)["autoally"]
        self.campo_mensaje = discord.ui.TextInput(
            label="Mensaje ({servername} y {bot_invite_url})",
            style=discord.TextStyle.paragraph,
            default=aa.get("mensaje_dm") or "",
            max_length=1800,
            required=True,
        )
        self.add_item(self.campo_mensaje)

    async def on_submit(self, interaction: discord.Interaction):
        gdata = get_guild_data(self.guild_id)
        gdata["autoally"]["mensaje_dm"] = self.campo_mensaje.value
        guardar_datos(data)
        await interaction.response.send_message("✅ Mensaje de auto-alianza actualizado.", ephemeral=True)


class AutoAllyRequisitosModal(discord.ui.Modal, title="Requisitos de auto-alianza"):
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        req = get_guild_data(guild_id)["autoally"]["requisitos"]

        self.campo_miembros = discord.ui.TextInput(
            label="Miembros mínimos (número, 0 = sin mínimo)",
            default=str(req.get("miembros_minimos", 0)),
            max_length=10,
            required=False,
        )
        self.campo_nsfw = discord.ui.TextInput(
            label="¿Permitir canales NSFW? (si/no)",
            default="si" if req.get("permitir_nsfw", True) else "no",
            max_length=5,
            required=False,
        )
        self.campo_antiguedad = discord.ui.TextInput(
            label="Antigüedad mín. cuenta dueño (días)",
            default=str(req.get("antiguedad_cuenta_dueno_dias", 0)),
            max_length=10,
            required=False,
        )
        self.campo_verificado = discord.ui.TextInput(
            label="¿Requiere Verificado/Partner? (si/no)",
            default="si" if req.get("servidor_verificado_o_partner") else "no",
            max_length=5,
            required=False,
        )
        self.campo_palabras = discord.ui.TextInput(
            label="Palabras prohibidas (separadas por coma)",
            style=discord.TextStyle.paragraph,
            default=", ".join(req.get("palabras_prohibidas", [])),
            max_length=500,
            required=False,
        )
        for campo in (self.campo_miembros, self.campo_nsfw, self.campo_antiguedad, self.campo_verificado, self.campo_palabras):
            self.add_item(campo)

    async def on_submit(self, interaction: discord.Interaction):
        gdata = get_guild_data(self.guild_id)
        req = gdata["autoally"]["requisitos"]

        try:
            req["miembros_minimos"] = max(0, int(self.campo_miembros.value.strip() or "0"))
        except ValueError:
            req["miembros_minimos"] = 0

        req["permitir_nsfw"] = self.campo_nsfw.value.strip().lower() not in ("no", "n", "false", "0")

        try:
            req["antiguedad_cuenta_dueno_dias"] = max(0, int(self.campo_antiguedad.value.strip() or "0"))
        except ValueError:
            req["antiguedad_cuenta_dueno_dias"] = 0

        req["servidor_verificado_o_partner"] = self.campo_verificado.value.strip().lower() in ("si", "sí", "s", "true", "1")

        palabras = [p.strip() for p in self.campo_palabras.value.split(",") if p.strip()]
        req["palabras_prohibidas"] = palabras

        guardar_datos(data)
        await interaction.response.send_message("✅ Requisitos de auto-alianza actualizados.", ephemeral=True)


class ToggleAutoAllyButton(discord.ui.Button):
    def __init__(self, guild_id: int):
        aa = get_guild_data(guild_id)["autoally"]
        activo = aa.get("activo", False)
        super().__init__(
            label="Desactivar auto-alianza" if activo else "Activar auto-alianza",
            style=discord.ButtonStyle.green if not activo else discord.ButtonStyle.red,
            emoji="🔁",
            row=2,
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        gdata = get_guild_data(self.guild_id)
        aa = gdata["autoally"]
        if not aa.get("invite_url"):
            await interaction.response.send_message("⚠️ Primero configurá la URL de invitación antes de activar la auto-alianza.", ephemeral=True)
            return
        if not gdata.get("canal_alianzas"):
            await interaction.response.send_message("⚠️ Primero configurá el canal de alianzas con `/allychannel`.", ephemeral=True)
            return
        aa["activo"] = not aa.get("activo", False)
        guardar_datos(data)
        self.label = "Desactivar auto-alianza" if aa["activo"] else "Activar auto-alianza"
        self.style = discord.ButtonStyle.red if aa["activo"] else discord.ButtonStyle.green
        await interaction.response.edit_message(content=texto_estado_autoally(gdata), view=self.view)


class AutoAllySetupView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(ToggleAutoAllyButton(guild_id))

    @discord.ui.button(label="URL de invitación", style=discord.ButtonStyle.blurple, emoji="🔗", row=0)
    async def config_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoAllyInviteModal(self.guild_id))

    @discord.ui.button(label="Mensaje de invitación (MD)", style=discord.ButtonStyle.blurple, emoji="✉️", row=0)
    async def config_mensaje(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoAllyMensajeModal(self.guild_id))

    @discord.ui.button(label="Requisitos", style=discord.ButtonStyle.grey, emoji="📋", row=1)
    async def config_requisitos(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoAllyRequisitosModal(self.guild_id))


# =============================================================================
# UI: solicitud de plantilla al server 2 (por MD)
# =============================================================================
class PlantillaModal(discord.ui.Modal, title="Plantilla de tu servidor"):
    descripcion_input = discord.ui.TextInput(
        label="Descripción/plantilla de tu servidor",
        style=discord.TextStyle.paragraph,
        placeholder="Contanos sobre tu servidor: temática, reglas destacadas, qué ofrece, etc.",
        max_length=2000,
        required=True,
    )
    invite_input = discord.ui.TextInput(
        label="Link de invitación de tu servidor",
        placeholder="https://discord.gg/tuinvite",
        max_length=200,
        required=True,
    )

    def __init__(self, on_submit_callback):
        super().__init__()
        self._callback = on_submit_callback

    async def on_submit(self, interaction: discord.Interaction):
        await self._callback(interaction, self.descripcion_input.value, self.invite_input.value)


class PlantillaSolicitudView(discord.ui.View):
    def __init__(self, on_submit_callback):
        super().__init__(timeout=1800)  # 30 min para completar
        self._callback = on_submit_callback

    @discord.ui.button(label="Enviar plantilla de mi servidor", style=discord.ButtonStyle.green, emoji="📨")
    async def enviar_plantilla(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlantillaModal(self._callback))


# =============================================================================
# UI: verificación de que el server 2 publicó la plantilla del server 1
# =============================================================================
class CanalPublicacionModal(discord.ui.Modal, title="Canal donde vas a publicar"):
    canal_input = discord.ui.TextInput(
        label="ID o link del canal",
        placeholder="Pegá el ID del canal o el link del mensaje/canal",
        max_length=200,
        required=True,
    )

    def __init__(self, on_submit_callback):
        super().__init__()
        self._callback = on_submit_callback

    async def on_submit(self, interaction: discord.Interaction):
        await self._callback(interaction, self.canal_input.value.strip())


class ElegirCanalView(discord.ui.View):
    """Paso 1: el admin indica en qué canal de SU servidor va a publicar la plantilla."""
    def __init__(self, on_canal_elegido):
        super().__init__(timeout=1800)  # 30 min para elegir canal, todavía no corre el cronómetro de 10
        self._callback = on_canal_elegido

    @discord.ui.button(label="Indicar canal", style=discord.ButtonStyle.blurple, emoji="📍")
    async def indicar_canal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CanalPublicacionModal(self._callback))


class ConfirmarPublicacionView(discord.ui.View):
    """Paso 2: una vez elegido el canal, el admin confirma por escrito que ya publicó
    la plantilla ahí. Recién en ese momento el bot busca el link y, si no lo encuentra,
    arranca el cronómetro de 10 minutos (con opción de reintentar o cambiar de canal)."""
    def __init__(self, canal: discord.TextChannel, on_confirmar, on_cambiar_canal):
        super().__init__(timeout=600)  # 10 minutos desde que se le ofrece confirmar
        self.canal = canal
        self._on_confirmar = on_confirmar
        self._on_cambiar_canal = on_cambiar_canal
        self.expirado = False

    @discord.ui.button(label="Ya la publiqué, verificar", style=discord.ButtonStyle.green, emoji="✅")
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.expirado:
            await interaction.response.send_message("⏱️ El tiempo para completar este paso ya venció.", ephemeral=True)
            return
        await self._on_confirmar(interaction, self.canal)

    @discord.ui.button(label="Cambiar de canal", style=discord.ButtonStyle.grey, emoji="🔄")
    async def cambiar_canal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.expirado:
            await interaction.response.send_message("⏱️ El tiempo para completar este paso ya venció.", ephemeral=True)
            return
        await interaction.response.send_modal(CanalPublicacionModal(self._on_cambiar_canal))


# =============================================================================
# UI: /allylist
# =============================================================================
class AllyListView(discord.ui.View):
    POR_PAGINA = 10

    def __init__(self, guild: discord.Guild, alianzas: list):
        super().__init__(timeout=120)
        self.guild = guild
        self.alianzas = alianzas
        self.pagina = 0
        self.paginas = max(1, math.ceil(len(alianzas) / self.POR_PAGINA))
        self._actualizar_botones()

    def _actualizar_botones(self):
        self.anterior.disabled = self.pagina <= 0
        self.siguiente.disabled = self.pagina >= self.paginas - 1

    def construir_embed(self) -> discord.Embed:
        inicio = self.pagina * self.POR_PAGINA
        fin = inicio + self.POR_PAGINA
        lote = self.alianzas[inicio:fin]

        lineas = []
        for entrada in lote:
            fecha = datetime.datetime.fromisoformat(entrada["fecha_iso"]).strftime("%d/%m/%Y %H:%M UTC")
            lineas.append(f"`#{entrada['id']}` **{entrada['servidor_aliado']}** — por <@{entrada['usuario_id']}> · {fecha}")

        embed = discord.Embed(
            title=f"🤝 Alianzas completadas — {self.guild.name}",
            description="\n".join(lineas) if lineas else "Sin alianzas en esta página.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Página {self.pagina + 1}/{self.paginas} · Total: {len(self.alianzas)} alianzas")
        return embed

    @discord.ui.button(label="◀️ Anterior", style=discord.ButtonStyle.grey)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina = max(0, self.pagina - 1)
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    @discord.ui.button(label="Siguiente ▶️", style=discord.ButtonStyle.grey)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina = min(self.paginas - 1, self.pagina + 1)
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)


# =============================================================================
# COMANDOS
# =============================================================================
@bot.tree.command(name="allynew", description="[Staff] Registra una alianza completada y publica el embed en el canal configurado.")
@app_commands.describe(servidor="Nombre del servidor con el que se hizo la alianza")
@app_commands.guild_only()
@staff_only()
async def allynew(interaction: discord.Interaction, servidor: str):
    gdata = get_guild_data(interaction.guild.id)
    canal_id = gdata.get("canal_alianzas")
    canal = interaction.guild.get_channel(canal_id) if canal_id else None
    if not canal:
        await interaction.response.send_message("⚠️ Todavía no configuraste el canal de alianzas. Usá `/allychannel` primero.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    gdata, entrada = registrar_alianza(interaction.guild, interaction.user, servidor.strip())

    try:
        await enviar_alerta_alianza(canal, interaction.guild, gdata, entrada, interaction.user.mention, servidor.strip())
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ No pude enviar el embed al canal configurado ({e}).", ephemeral=True)
        return

    await interaction.followup.send(f"✅ Alianza con **{servidor.strip()}** registrada y publicada en {canal.mention}.", ephemeral=True)


@allynew.error
async def allynew_error(interaction: discord.Interaction, error):
    await manejar_error_staff(interaction, error)


@bot.tree.command(name="allychannel", description="[Staff] Configura el canal donde se envían las alertas de alianzas.")
@app_commands.guild_only()
@staff_only()
async def allychannel(interaction: discord.Interaction):
    gdata = get_guild_data(interaction.guild.id)
    await interaction.response.send_message(texto_estado_canal(interaction.guild, gdata), view=AllyChannelView(interaction.guild.id), ephemeral=True)


@allychannel.error
async def allychannel_error(interaction: discord.Interaction, error):
    await manejar_error_staff(interaction, error)


@bot.tree.command(name="embedconfig", description="[Staff] Configura el embed que se envía en cada alianza.")
@app_commands.guild_only()
@staff_only()
async def embedconfig(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Configurá el embed de alianzas. Podés usar `{username}` y `{servername}` en el título, la descripción y el footer.",
        view=EmbedConfigView(interaction.guild.id),
        ephemeral=True,
    )


@embedconfig.error
async def embedconfig_error(interaction: discord.Interaction, error):
    await manejar_error_staff(interaction, error)


@bot.tree.command(name="autoallysetup", description="[Staff] Configura la auto-alianza: URL de invitación y requisitos para otros servidores.")
@app_commands.guild_only()
@staff_only()
async def autoallysetup(interaction: discord.Interaction):
    gdata = get_guild_data(interaction.guild.id)
    await interaction.response.send_message(texto_estado_autoally(gdata), view=AutoAllySetupView(interaction.guild.id), ephemeral=True)


@autoallysetup.error
async def autoallysetup_error(interaction: discord.Interaction, error):
    await manejar_error_staff(interaction, error)


@bot.tree.command(name="autoally", description="Solicita una auto-alianza con este servidor: recibirás instrucciones por MD.")
@app_commands.guild_only()
async def autoally(interaction: discord.Interaction):
    gdata = get_guild_data(interaction.guild.id)
    aa = gdata["autoally"]

    if not aa.get("activo"):
        await interaction.response.send_message("⚠️ Este servidor no tiene la auto-alianza activada.", ephemeral=True)
        return

    limpiar_sesiones_expiradas()

    bot_invite_url = discord.utils.oauth_url(
        bot.user.id,
        permissions=discord.Permissions(send_messages=True, embed_links=True, read_message_history=True, view_channel=True),
    )

    contexto = {
        "{servername}": interaction.guild.name,
        "{bot_invite_url}": bot_invite_url,
    }
    mensaje = reemplazar_placeholders(aa.get("mensaje_dm", ""), contexto)

    try:
        await interaction.user.send(mensaje)
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ No pude enviarte un mensaje directo. Revisá que tengas los MD abiertos para este servidor y volvé a intentar.",
            ephemeral=True,
        )
        return

    # Registramos la sesión: este usuario está en proceso de auto-alianza con este guild (server 1).
    # Todavía no sabemos cuál es su servidor (server 2); se completa cuando el bot entre ahí.
    autoally_sesiones[interaction.user.id] = {
        "guild_origen_id": interaction.guild.id,
        "creado": discord.utils.utcnow(),
    }
    guardar_sesiones_autoally()

    await interaction.response.send_message(
        "✅ Te mandé un mensaje directo con las instrucciones. Agregá el bot a **tu servidor** "
        "siguiendo el link que te envié para continuar con la auto-alianza.",
        ephemeral=True,
    )


@autoally.error
async def autoally_error(interaction: discord.Interaction, error):
    await manejar_error_staff(interaction, error)


@bot.tree.command(name="allylist", description="Muestra la lista de alianzas completadas en este servidor.")
@app_commands.guild_only()
async def allylist(interaction: discord.Interaction):
    gdata = get_guild_data(interaction.guild.id)
    alianzas = list(reversed(gdata.get("alianzas", [])))
    if not alianzas:
        await interaction.response.send_message("Todavía no se registraron alianzas en este servidor.", ephemeral=True)
        return

    view = AllyListView(interaction.guild, alianzas)
    await interaction.response.send_message(embed=view.construir_embed(), view=view, ephemeral=True)


@allylist.error
async def allylist_error(interaction: discord.Interaction, error):
    await manejar_error_staff(interaction, error)


@bot.tree.command(name="allyhelp", description=f"Guía rápida de configuración de {BOT_NAME}.")
async def allyhelp(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"Guía de {BOT_NAME}",
        description="Sistema simple de alianzas: registra alianzas manualmente o detectándolas automáticamente.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="🖱️ /allychannel", value="Elegí el canal donde se publican las alertas y activá/desactivá la detección automática de plantillas.", inline=False)
    embed.add_field(name="🖱️ /embedconfig", value="Personalizá título, descripción, color, imagen, footer y el rol que se menciona en cada alianza. Usá `{username}` y `{servername}` como variables.", inline=False)
    embed.add_field(name="🤝 /allynew servidor:<nombre>", value="Solo staff/admins. Registra la alianza manualmente y publica el embed.", inline=False)
    embed.add_field(name="📋 /allylist", value="Muestra todas las alianzas completadas en este servidor, paginadas.", inline=False)
    embed.add_field(
        name="🤖 /autoallysetup",
        value="Solo staff/admins. Configura la URL de invitación, el mensaje de MD y los requisitos (miembros mínimos, NSFW, antigüedad de cuenta, palabras prohibidas, verificado/partner) para aceptar auto-alianzas de otros servidores.",
        inline=False,
    )
    embed.add_field(
        name="🌐 /autoally",
        value=(
            "Cualquier admin de otro servidor puede usarlo para pedir una auto-alianza. El bot le manda por MD "
            "el link de invitación; al unirse, se valida contra los requisitos configurados. Si pasa, se le pide "
            "la plantilla de su server (se publica acá) y luego debe publicar la plantilla de este server en el "
            "suyo e indicar el canal — el bot lo verifica en 10 minutos o revierte la alianza."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔄 Detección automática",
        value="Si está activada, cuando alguien manda en el canal configurado un embed de otro server o un link de invitación, TaoAlly registra la alianza solo y publica el mismo embed.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =============================================================================
# AUTO-ALIANZA: FLUJO COMPLETO CUANDO EL BOT ENTRA A UN NUEVO SERVIDOR
# =============================================================================
async def rechazar_autoally(admin: discord.abc.User, guild_origen: discord.Guild, guild_candidato: discord.Guild, motivos: list):
    texto_motivos = "\n".join(f"• {m}" for m in motivos)
    mensaje = (
        f"❌ Tu solicitud de auto-alianza con **{guild_origen.name}** fue **rechazada**.\n\n"
        f"**Motivos:**\n{texto_motivos}\n\n"
        "Podés corregir estos puntos en tu servidor y volver a intentarlo más adelante."
    )
    try:
        await admin.send(mensaje)
    except discord.HTTPException:
        pass
    try:
        await guild_candidato.leave()
    except discord.HTTPException:
        pass


async def pedir_plantilla_y_completar(admin: discord.abc.User, guild_origen: discord.Guild, guild_candidato: discord.Guild):
    async def al_recibir_plantilla(interaction: discord.Interaction, descripcion: str, invite_link: str):
        print(f"[auto-alianza] Plantilla recibida de {admin} (server candidato: {guild_candidato.name}) "
              f"-> descripcion_len={len(descripcion or '')}, invite_link={invite_link!r}")

        gdata_origen = get_guild_data(guild_origen.id)
        canal_id = gdata_origen.get("canal_alianzas")
        canal_destino = guild_origen.get_channel(canal_id) if canal_id else None

        if not canal_destino:
            print(f"[auto-alianza] ABORTA: {guild_origen.name} no tiene canal_alianzas configurado (canal_id={canal_id}).")
            await interaction.response.send_message(
                "⚠️ Hubo un problema: el servidor no tiene canal de alianzas configurado. Contactá al staff.",
                ephemeral=True,
            )
            return

        aa_origen = gdata_origen["autoally"]
        invite_url_origen = aa_origen.get("invite_url")
        if not invite_url_origen:
            print(f"[auto-alianza] ABORTA: {guild_origen.name} no tiene invite_url configurada en /autoallysetup.")
            await interaction.response.send_message(
                "⚠️ Hubo un problema: el servidor no tiene una URL de invitación configurada con /autoallysetup. Contactá al staff.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "✅ ¡Gracias! Tu plantilla fue publicada. Ahora falta el último paso.",
            ephemeral=True,
        )

        plantilla_embed = discord.Embed(
            title=f"🌐 {guild_candidato.name}",
            description=descripcion,
            color=discord.Color.blurple(),
        )
        if guild_candidato.icon:
            plantilla_embed.set_thumbnail(url=guild_candidato.icon.url)
        plantilla_embed.add_field(name="🔗 Invitación", value=invite_link, inline=False)
        plantilla_embed.add_field(name="👥 Miembros", value=str(guild_candidato.member_count or "N/D"), inline=True)

        gdata_origen, entrada = registrar_alianza(guild_origen, admin, guild_candidato.name)
        print(f"[auto-alianza] Publicando en {guild_origen.name}#{canal_destino.name} "
              f"(embed principal + plantilla embed).")
        try:
            mensajes_publicados = await enviar_alerta_alianza(
                canal_destino, guild_origen, gdata_origen, entrada,
                getattr(admin, "mention", str(admin)), guild_candidato.name,
                es_auto=True, plantilla_embed=plantilla_embed,
            )
            print(f"[auto-alianza] Publicados {len(mensajes_publicados)} mensaje(s) en {canal_destino.name}.")
        except Exception as e:
            print(f"[auto-alianza] ERROR al publicar la auto-alianza: {type(e).__name__}: {e}")
            mensajes_publicados = []

        # Paso final: pedirle al admin del server 2 que publique la plantilla del
        # server 1 (la URL configurada con /autoallysetup) en un canal de su server,
        # y verificar que efectivamente esté ahí. Si no, se revierte todo.
        await solicitar_publicacion_reciproca(
            admin, guild_origen, guild_candidato, invite_url_origen,
            gdata_origen, entrada, mensajes_publicados,
        )

    try:
        await admin.send(
            f"✅ ¡Tu servidor **{guild_candidato.name}** cumple con todos los requisitos de **{guild_origen.name}**!\n\n"
            "Para completar la alianza, mandanos la plantilla de tu servidor con el botón de abajo:",
            view=PlantillaSolicitudView(al_recibir_plantilla),
        )
    except discord.Forbidden:
        # No podemos avisarle por MD; igual dejamos la sesión activa por si escribe algo,
        # pero no hay mucho más que hacer sin poder contactarlo.
        pass


async def revertir_publicacion(mensajes_publicados: list, gdata_origen: dict, entrada: dict):
    """Borra los mensajes publicados en el server 1 y revierte el registro de la alianza."""
    for msg in mensajes_publicados:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
    try:
        gdata_origen["alianzas"] = [a for a in gdata_origen["alianzas"] if a["id"] != entrada["id"]]
        gdata_origen["contador_alianzas"] = max(0, gdata_origen["contador_alianzas"] - 1)
        guardar_datos(data)
    except (KeyError, ValueError):
        pass


async def solicitar_publicacion_reciproca(
    admin: discord.abc.User,
    guild_origen: discord.Guild,
    guild_candidato: discord.Guild,
    invite_url_origen: str,
    gdata_origen: dict,
    entrada: dict,
    mensajes_publicados: list,
):
    async def buscar_y_verificar(interaction: discord.Interaction, canal: discord.TextChannel):
        """Se llama cuando el admin confirma por escrito que ya publicó. Acá recién
        el bot busca en el canal. Si no la encuentra, arranca (o ya está corriendo)
        el cronómetro de 10 minutos."""
        encontrado = await buscar_invite_en_canal(canal, invite_url_origen)

        if encontrado:
            await interaction.response.send_message(
                f"✅ ¡Listo! Encontré la plantilla de **{guild_origen.name}** en {canal.mention}. "
                "¡Alianza confirmada por ambas partes! 🎉",
                ephemeral=True,
            )
            timeout_task = tareas_verificacion.pop(admin.id, None)
            if timeout_task:
                timeout_task.cancel()
            autoally_sesiones.pop(admin.id, None)
            guardar_sesiones_autoally()
            return

        # No se encontró: si todavía no había cronómetro corriendo para este admin, lo arrancamos ahora.
        ya_habia_cronometro = admin.id in tareas_verificacion
        await interaction.response.send_message(
            f"⚠️ No encontré el link de invitación de **{guild_origen.name}** en {canal.mention}.\n"
            "Revisá que la hayas publicado ahí (link o embed completo) y volvé a confirmar, "
            "o indicá otro canal con «Cambiar de canal».\n\n"
            + ("Seguís dentro de la ventana de 10 minutos." if ya_habia_cronometro
               else "Arranca ahora un plazo de **10 minutos** para completar este paso."),
            ephemeral=True,
        )
        if not ya_habia_cronometro:
            tareas_verificacion[admin.id] = asyncio.ensure_future(al_expirar())

    async def al_confirmar_publicacion(interaction: discord.Interaction, canal: discord.TextChannel):
        await buscar_y_verificar(interaction, canal)

    async def al_elegir_o_cambiar_canal(interaction: discord.Interaction, texto_canal: str):
        canal = resolver_canal_por_texto(guild_candidato, texto_canal)
        if canal is None or not isinstance(canal, discord.TextChannel):
            await interaction.response.send_message(
                "❌ No encontré ese canal en tu servidor. Verificá el ID o el link y probá de nuevo.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"📍 Canal registrado: {canal.mention}.\n\n"
            f"Publicá ahí la plantilla de **{guild_origen.name}** "
            f"(link de invitación: {invite_url_origen}) y después tocá **«Ya la publiqué, verificar»**.",
            view=ConfirmarPublicacionView(canal, al_confirmar_publicacion, al_elegir_o_cambiar_canal),
            ephemeral=True,
        )

    async def al_expirar():
        await asyncio.sleep(600)  # 10 minutos desde que se detectó que la plantilla no estaba
        if admin.id not in tareas_verificacion:
            return  # ya se verificó con éxito o se canceló
        tareas_verificacion.pop(admin.id, None)
        await revertir_publicacion(mensajes_publicados, gdata_origen, entrada)
        try:
            await admin.send(
                f"❌ No se detectó la publicación de la plantilla de **{guild_origen.name}** en tu servidor "
                f"dentro de los 10 minutos. La auto-alianza fue **cancelada** y se eliminó la publicación "
                f"de tu servidor en **{guild_origen.name}**."
            )
        except discord.HTTPException:
            pass
        autoally_sesiones.pop(admin.id, None)
        guardar_sesiones_autoally()

    try:
        await admin.send(
            f"📢 Último paso: elegí en qué canal de **tu servidor** vas a publicar la plantilla de "
            f"**{guild_origen.name}**.",
            view=ElegirCanalView(al_elegir_o_cambiar_canal),
        )
    except discord.Forbidden:
        # No podemos contactar al admin; revertimos directamente.
        await revertir_publicacion(mensajes_publicados, gdata_origen, entrada)
        autoally_sesiones.pop(admin.id, None)
        guardar_sesiones_autoally()
        return


@bot.event
async def on_guild_join(guild: discord.Guild):
    limpiar_sesiones_expiradas()

    # Buscamos si algún admin con sesión de auto-alianza pendiente está en este nuevo servidor.
    candidato_admin_id = None
    for uid, sesion in list(autoally_sesiones.items()):
        miembro = guild.get_member(uid)
        if miembro is not None:
            candidato_admin_id = uid
            break

    if candidato_admin_id is None:
        return  # no hay ninguna solicitud de auto-alianza asociada a este ingreso

    sesion = autoally_sesiones[candidato_admin_id]
    guild_origen = bot.get_guild(sesion["guild_origen_id"])
    if guild_origen is None:
        autoally_sesiones.pop(candidato_admin_id, None)
        guardar_sesiones_autoally()
        return

    admin = guild.get_member(candidato_admin_id)
    if admin is None:
        return

    # El que invitó al bot a server 2 debería ser admin/dueño ahí también.
    es_admin_candidato = (
        guild.owner_id == admin.id
        or admin.guild_permissions.administrator
        or admin.guild_permissions.manage_guild
    )
    if not es_admin_candidato:
        try:
            await admin.send(
                f"❌ Tu solicitud de auto-alianza con **{guild_origen.name}** fue rechazada: "
                "no tenés permisos de administrador en el servidor donde agregaste el bot."
            )
        except discord.HTTPException:
            pass
        try:
            await guild.leave()
        except discord.HTTPException:
            pass
        autoally_sesiones.pop(candidato_admin_id, None)
        guardar_sesiones_autoally()
        return

    gdata_origen = get_guild_data(guild_origen.id)
    requisitos = gdata_origen["autoally"]["requisitos"]
    motivos = evaluar_requisitos_autoally(guild, requisitos)

    if motivos:
        await rechazar_autoally(admin, guild_origen, guild, motivos)
        autoally_sesiones.pop(candidato_admin_id, None)
        guardar_sesiones_autoally()
        return

    await pedir_plantilla_y_completar(admin, guild_origen, guild)


# =============================================================================
# DETECCION AUTOMATICA DE PLANTILLAS
# =============================================================================
@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return
    if not message.guild:
        return

    gdata = get_guild_data(message.guild.id)
    if not gdata.get("deteccion_automatica", True):
        return

    canal_id = gdata.get("canal_alianzas")
    if not canal_id or message.channel.id != canal_id:
        return

    # Si este autor tiene una auto-alianza en curso con ESTE servidor, no dejamos que
    # la detección automática "manual" interfiera: ese caso lo maneja exclusivamente
    # el flujo controlado de /autoally (pedir_plantilla_y_completar), que ya se encarga
    # de publicar el embed marcado como auto-alianza en el momento correcto.
    sesion_autoally = autoally_sesiones.get(message.author.id)
    if sesion_autoally and sesion_autoally.get("guild_origen_id") == message.guild.id:
        return

    servidor_aliado = await detectar_servidor_aliado(bot, message)
    if not servidor_aliado:
        return

    gdata, entrada = registrar_alianza(message.guild, message.author, servidor_aliado)
    try:
        await enviar_alerta_alianza(message.channel, message.guild, gdata, entrada, message.author.mention, servidor_aliado)
    except discord.HTTPException as e:
        print(f"No se pudo enviar la alerta automática de alianza: {e}")


@bot.event
async def on_ready():
    ahora_str = discord.utils.utcnow().strftime("%d/%m/%Y %H:%M:%S UTC")

    # on_ready puede dispararse más de una vez por proceso (reconexiones de Discord);
    # sincronizamos los comandos solo la primera vez que el bot queda listo.
    if not bot.ya_sincronizo:
        bot.ya_sincronizo = True

        comandos_antes = len(bot.tree.get_commands())
        synced = None
        intentos = 0
        while synced is None and intentos < 3:
            intentos += 1
            try:
                synced = await bot.tree.sync()
            except discord.HTTPException as e:
                if e.status == 429 and intentos < 3:
                    espera = getattr(e, "retry_after", 5) or 5
                    print(f"[{ahora_str}] Rate limit al sincronizar comandos, reintentando en {espera}s...")
                    await asyncio.sleep(espera)
                else:
                    print(f"[{ahora_str}] Error al sincronizar comandos: {e}")
                    break
            except Exception as e:
                print(f"[{ahora_str}] Error inesperado al sincronizar comandos: {e}")
                break

        if synced is not None:
            print(f"[{ahora_str}] Auto-sync: {len(synced)} comandos slash sincronizados (registrados en código: {comandos_antes}).")
        else:
            print(f"[{ahora_str}] Auto-sync FALLÓ tras {intentos} intento(s). Los comandos pueden estar desactualizados hasta el próximo reinicio.")

        # Restauramos sesiones de auto-alianza que hayan quedado pendientes de un
        # apagado anterior. Los cronómetros de 10 minutos no sobreviven a un reinicio,
        # así que avisamos a esos admins para que retomen el paso de verificación.
        cargar_sesiones_autoally()
        limpiar_sesiones_expiradas()
        for uid, sesion in list(autoally_sesiones.items()):
            guild_origen = bot.get_guild(sesion["guild_origen_id"])
            usuario = bot.get_user(uid)
            if guild_origen is None or usuario is None:
                continue
            try:
                await usuario.send(
                    f"🔄 El bot se reinició mientras tenías una auto-alianza en curso con "
                    f"**{guild_origen.name}**. Si ya habías agregado el bot a tu servidor, "
                    f"usá `/autoally` de nuevo en {guild_origen.name} para retomar el proceso."
                )
            except discord.HTTPException:
                pass

    print(f"[{ahora_str}] Conectado como {bot.user}. {BOT_NAME} listo!")


if __name__ == "__main__":
    bot.run(TOKEN)
