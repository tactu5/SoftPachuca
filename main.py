import os
import logging
import httpx
import asyncio
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import PlainTextResponse
from fastapi.concurrency import run_in_threadpool
from dotenv import load_dotenv
from mysql.connector import pooling, Error as MySQLError, IntegrityError

# Configuración
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Bot Pachuca SaaS", version="3.2.0")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
N8N_DEFAULT_URL = os.getenv("N8N_WEBHOOK_URL")

# --- POOL DE CONEXIONES ---
try:
    db_pool = pooling.MySQLConnectionPool(
        pool_name="whatsapp_pool",
        pool_size=5,
        pool_reset_session=True,
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )
    logger.info("✅ Pool MySQL Multi-Tenant listo.")
except MySQLError as e:
    logger.critical(f"❌ Error Pool BD: {e}")
    exit(1)

# --- UTILIDADES ---

def limpiar_telefono_mexico(telefono: str) -> str:
    """Normaliza números mexicanos quitando el '1' después del 52."""
    if telefono.startswith("521") and len(telefono) == 13:
        return telefono.replace("521", "52", 1)
    return telefono

def obtener_db_conexion():
    """Helper para obtener conexión del pool de forma segura."""
    return db_pool.get_connection()

def es_spam(telefono):
    """Verifica si el número está en la tabla blacklist."""
    conexion = None
    try:
        conexion = obtener_db_conexion()
        cursor = conexion.cursor()
        cursor.execute("SELECT 1 FROM blacklist WHERE telefono = %s", (telefono,))
        resultado = cursor.fetchone()
        return resultado is not None # Devuelve True si está bloqueado
    except Exception as e:
        logger.error(f"❌ Error revisando blacklist: {e}")
        return False # Ante la duda, dejar pasar (fail-open)
    finally:
        if conexion and conexion.is_connected():
            cursor.close()
            conexion.close()

def _db_obtener_negocio(telefono_id_meta):
    """
    Busca el negocio usando tu columna exacta: 'telefono_id_meta'
    """
    conexion = None
    try:
        conexion = db_pool.get_connection()
        cursor = conexion.cursor()
        # AJUSTE: Usamos tus nombres de columnas exactos
        sql = "SELECT id, nombre, webhook_n8n, acces_token FROM negocios WHERE telefono_id_meta = %s AND activo = 1"
        cursor.execute(sql, (telefono_id_meta,))
        return cursor.fetchone()
    except MySQLError as err:
        logger.error(f"❌ Error buscando negocio: {err}")
        return None
    finally:
        if conexion and conexion.is_connected():
            cursor.close()
            conexion.close()

def _db_guardar_mensaje(negocio_id, message_id, telefono, nombre, mensaje_formateado):
    """
    Guarda en la tabla 'mensajes'. 
    NOTA: Como no tienes columna 'tipo_mensaje', guardamos todo en 'texto_mensaje'.
    """
    conexion = None
    try:
        conexion = db_pool.get_connection()
        cursor = conexion.cursor()
        # AJUSTE: Quitamos 'tipo_mensaje' del INSERT porque tu tabla no lo tiene
        sql = """
        INSERT INTO mensajes (negocio_id, message_id, telefono, nombre_cliente, texto_mensaje) 
        VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(sql, (negocio_id, message_id, telefono, nombre, mensaje_formateado))
        conexion.commit()
        logger.info(f"💾 Guardado para Negocio #{negocio_id}")
        return True
    except IntegrityError:
        logger.warning(f"♻️ Duplicado ignorado: {message_id}")
        return False
    except MySQLError as err:
        logger.error(f"❌ Error BD: {err}")
        return False
    finally:
        if conexion and conexion.is_connected():
            cursor.close()
            conexion.close()

async def notificar_n8n(url_webhook, negocio_nombre, telefono, nombre, tipo, mensaje_original, id_emisor_wa, negocio_token):
    """Envía a n8n."""
    target_url = url_webhook if url_webhook else N8N_DEFAULT_URL
    if not target_url: return

    payload = {
        "negocio": negocio_nombre,
        "telefono": telefono,
        "nombre": nombre,
        "tipo": tipo, # Enviamos el tipo a n8n para que sepa qué hacer
        "mensaje": mensaje_original,
        "id_emisor": id_emisor_wa,
        "negocio_token": negocio_token
    }
    
    max_retires = 3
    for intento in range(max_retires):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(target_url, json=payload, timeout=10.0)
                if resp.status_code == 200:
                    logger.info(f"🚀 Enviado a n8n ({negocio_nombre}) OK")
                    return
                else:
                    logger.warning(f"⚠️ n8n error: {resp.status_code}")
        except Exception as e:
            logger.error(f"❌ Error n8n: {e} (Intento {intento+1}/{max_retires})")
        
        if intento<max_retires-1:
            wait_time = 2**intento
            await asyncio.sleep(wait_time)

# --- RUTAS ---

@app.get("/health")
def health_check():
    return {"status": "ok", "mode": "multi-tenant-retry"}

@app.get("/webhook")
async def verificar_token(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=params.get("hub.challenge"), status_code=200)
    raise HTTPException(status_code=403, detail="Token incorrecto")

@app.post("/webhook")
async def recibir_mensaje(request: Request):
    try:
        cuerpo = await request.json()
        
        entry = cuerpo.get("entry", [])
        if not entry: return {"status": "ignored"}
        changes = entry[0].get("changes", [])
        if not changes: return {"status": "ignored"}
        value = changes[0].get("value", {})
        
        if 'messages' not in value or not value['messages']:
            return {"status": "ignored_no_message_content"}
            # 1. IDENTIFICAR AL NEGOCIO
        metadata = value.get('metadata', {})
        phone_id_meta = metadata.get('phone_number_id')
        
        # Buscamos usando tu columna telefono_id_meta
        negocio = await run_in_threadpool(_db_obtener_negocio, phone_id_meta)
        
        if not negocio:
            logger.warning(f"⚠️ ID desconocido: {phone_id_meta}")
            return {"status": "unknown_business"}
        
        negocio_id, negocio_nombre, negocio_webhook, negocio_token = negocio

        # 2. PROCESAR MENSAJE
        msg = value['messages'][0]
        msg_id = msg.get("id")
        if not msg_id: return {"status": "ignored_no_id"}

        contact = value.get('contacts', [{}])[0]
        nombre = contact.get('profile', {}).get('name', 'Desconocido')
        telefono = limpiar_telefono_mexico(msg.get('from', ''))
        
        if es_spam(telefono):
            logger.warning(f"🚫 SPAM DETECTADO: El número {telefono} intentó escribir.")
            return {"status": "ignored_spam"}
        
        # Lógica de tipos
        msg_type = msg.get('type')
        contenido_db = "" # Lo que guardamos en BD (texto combinado)
        contenido_n8n = "" # Lo que mandamos a n8n (dato crudo)

        if msg_type == 'text':
            raw_text = msg.get('text', {}).get('body', '')
            contenido_db = raw_text
            contenido_n8n = raw_text
        elif msg_type == 'interactive':
            interactive_type = msg.get('interactive', {}).get('type')
            
            if interactive_type == 'button_reply':
                btn_title = msg.get('interactive', {}).get('button_reply', {}).get('title', '')
                btn_id = msg.get('interactive', {}).get('button_reply', {}).get('id', '')
                contenido_db = btn_title
                contenido_n8n = btn_title
                
            elif interactive_type == 'list_reply':
                list_title = msg.get('interactive', {}).get('list_reply', {}).get('title', '')
                contenido_db = list_title
                contenido_n8n = list_title
            
            else :
                contenido_db = "[INTERACCION DESCONOCIDA]"
                contenido_n8n = "unknown_interaction"
            
        elif msg_type == 'image':
            caption = msg.get('image', {}).get('caption', '')
            # Truco: Guardamos el tipo como prefijo en el texto
            contenido_db = f"[IMAGEN] {caption}".strip() 
            contenido_n8n = caption
        elif msg_type == 'audio':
            contenido_db = "[AUDIO]"
            contenido_n8n = ""
        elif msg_type == 'document':
                filename = msg.get('document', {}).get('filename', '')
                contenido_db = f"[DOC] {filename}"
                contenido_n8n = filename
        else:
            contenido_db = f"[{msg_type.upper()}]"
            contenido_n8n = ""

        # 3. GUARDAR EN BD
        # Usamos contenido_db que ya trae el prefijo [IMAGEN] si aplica
        es_nuevo = await run_in_threadpool(
            _db_guardar_mensaje, 
            negocio_id, msg_id, telefono, nombre, contenido_db
        )
        
        # 4. NOTIFICAR A N8N
        if es_nuevo:
            await notificar_n8n(negocio_webhook, negocio_nombre, telefono, nombre, msg_type, contenido_n8n, phone_id_meta, negocio_token)
                
        return {"status": "received"}
        
    except Exception as e:
        logger.error(f"❌ Error Webhook Critico: {e}", exc_info=True)
        return {"status": "error_handled"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")