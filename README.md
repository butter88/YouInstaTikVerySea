# BotTGInst — Bot de Telegram para descargar videos de TikTok

Bot que descarga y envia videos de TikTok directamente en chats/grupos de Telegram.

## Funcionalidades

- `/video <enlace>` — Descarga y envia el video del enlace de TikTok.
- **Deteccion automatica** — Si alguien pega un enlace de TikTok en el grupo, el bot lo detecta y responde con el video sin necesidad de comando.

## Requisitos

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) instalado y en el PATH (necesario para yt-dlp)

## Instalacion

```bash
# 1. Clonar e instalar dependencias
pip install -r requirements.txt

# 2. Crear tu bot en Telegram
#    Habla con @BotFather en Telegram, usa /newbot, y copia el token.

# 3. Configurar el token
#    Edita el archivo .env y pon tu token:
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...

# 4. (Opcional) Cookies de Instagram para evitar rate-limit
#    Exporta tus cookies de Instagram en formato Netscape (usa la extension
#    "Get cookies.txt LOCALLY" en Chrome), luego codifícalas en base64:
#      base64 -w0 cookies.txt
#    Pega el resultado en .env:
INSTAGRAM_COOKIES=<base64 del archivo cookies.txt>

# 5. Ejecutar el bot
python bot.py
```

## Anadir al grupo

1. Abre el grupo en Telegram.
2. Pulsa en el nombre del grupo → **Anadir miembros** → busca tu bot por su username.
3. (Recomendado) Hazlo **administrador** para que pueda leer todos los mensajes y borrar sus mensajes de estado.

## Notas

- Telegram limita el envio de archivos a **50 MB**. Videos mas grandes no se podran enviar.
- El bot usa `yt-dlp` que se actualiza frecuentemente. Si deja de funcionar, actualiza: `pip install -U yt-dlp`.
- Para Instagram, si el bot está en un servidor cloud, necesitarás configurar `INSTAGRAM_COOKIES` con las cookies de una sesión de Instagram para evitar bloqueos por rate-limit.
