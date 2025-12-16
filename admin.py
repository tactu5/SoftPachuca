import os
import mysql.connector
from dotenv import load_dotenv

# Cargar entorno (aunque en Docker ya vienen cargadas, esto ayuda si pruebas local)
load_dotenv()

def conectar_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "db"), # 'db' es el host dentro de Docker
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        charset='utf8mb4',
        collation='utf8mb4_unicode_ci'
    )

def listar_clientes():
    try:
        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, nombre, telefono_id_meta, activo FROM negocios")
        clientes = cursor.fetchall()
        
        print("\n--- 📋 CLIENTES REGISTRADOS ---")
        print(f"{'ID':<5} {'NOMBRE':<30} {'ID TELEFONO':<20} {'ACTIVO'}")
        print("-" * 70)
        for c in clientes:
            estado = "✅" if c[3] == 1 else "❌"
            print(f"{c[0]:<5} {c[1]:<30} {c[2]:<20} {estado}")
        print("-" * 70)
        
    except Exception as e:
        print(f"❌ Error al listar: {e}")
    finally:
        if 'conn' in locals() and conn.is_connected(): conn.close()

def bloquear_usuario():
    print("\n--- 🚫 BLOQUEAR USUARIO ---")
    telefono = input("Teléfono a bloquear (solo números): ").strip()
    motivo = input("Motivo: ").strip()
    
    confirmar = input(f"¿Seguro de bloquear a {telefono}? (s/n): ")
    if confirmar.lower() == 's':
        try:
            conn = conectar_db()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO blacklist (telefono, motivo) VALUES (%s, %s)", (telefono, motivo))
            conn.commit()
            print(f"💀 Número {telefono} enviado a la dimensión desconocida.")
        except Exception as e:
            print(f"Error: {e}")
        finally:
             if 'conn' in locals() and conn.is_connected(): conn.close()

def agregar_cliente():
    print("\n--- ➕ AGREGAR NUEVO CLIENTE ---")
    nombre = input("Nombre del Negocio: ").strip()
    telefono_id = input("ID de Teléfono (Meta): ").strip()
    token = input("Token Permanente (Meta): ").strip()
    
    # Generamos el webhook automáticamente basado en el nombre (limpiando espacios)
    slug = nombre.lower().replace(" ", "-")
    webhook = f"http://cerebro-n8n:5678/webhook/{slug}"
    
    print("\nResumen:")
    print(f"Empresa: {nombre}")
    print(f"Webhook: {webhook}")
    confirmar = input("¿Guardar en Base de Datos? (s/n): ")
    
    if confirmar.lower() == 's':
        try:
            conn = conectar_db()
            cursor = conn.cursor()
            sql = """INSERT INTO negocios (nombre, telefono_id_meta, webhook_n8n, acces_token, activo) 
                     VALUES (%s, %s, %s, %s, 1)"""
            cursor.execute(sql, (nombre, telefono_id, webhook, token))
            conn.commit()
            print(f"✅ ¡{nombre} agregado exitosamente!")
        except mysql.connector.Error as err:
            print(f"❌ Error SQL: {err}")
        finally:
            if 'conn' in locals() and conn.is_connected(): conn.close()
    else:
        print("❌ Operación cancelada.")

def menu():
    while True:
        print("\n🤖 --- PANEL DE CONTROL SOFTPACHUCA ---")
        print("1. Listar Clientes")
        print("2. Agregar Cliente")
        print("3. Bloquear numero")
        print("4. Salir")
        
        opcion = input("Selecciona una opción: ")
        
        if opcion == '1':
            listar_clientes()
        elif opcion == '2':
            agregar_cliente()
        elif opcion == '3':
            bloquear_usuario()
        elif opcion == '4':
            print("👋 ¡Adiós!")
            break
        else:
            print("Opción no válida.")

if __name__ == "__main__":
    # Verificamos si estamos dentro de Docker o fuera
    # Si DB_HOST no está, avisamos que debe correrse vía docker exec
    if not os.getenv("DB_HOST"):
        print("⚠️  ADVERTENCIA: Parece que estás ejecutando esto fuera del contenedor.")
        print("Para que funcione, usa el comando:")
        print("docker compose exec bot-pachuca python admin.py")
    else:
        menu()