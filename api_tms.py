from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware  
import os
import requests
import uvicorn
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
from geopy.geocoders import ArcGIS
import time
import math
import uuid # <-- NUEVO: Para crear nombres únicos de fotos
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

app = FastAPI(title="API TMS - Torre de Control (SaaS Pro)")

# ==========================================
# PASAPORTE CORS PARA FLUTTER
# ==========================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# CREDENCIALES DE LA NUBE
# ==========================================
URL_BASE_DATOS = "postgresql://postgres.xewyromxoprwvtkqveiw:rTY3rCcKVQk6yc2b@aws-1-sa-east-1.pooler.supabase.com:6543/postgres"

# ==========================================
# 1. FUNCIONES BASE (LECTURA Y ESTADOS)
# ==========================================
def obtener_ruta_db(patente: str):
    conexion = psycopg2.connect(URL_BASE_DATOS)
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute("""
        SELECT id_despacho, orden, direccion, tipo, estado 
        FROM rutas_asignadas 
        WHERE patente = %s 
        ORDER BY orden ASC
    """, (patente,))
    
    filas = cursor.fetchall()
    conexion.close()
    
    return [dict(fila) for fila in filas]

@app.get("/ruta/{patente}")
def descargar_ruta(patente: str):
    ruta = obtener_ruta_db(patente)
    if not ruta:
        raise HTTPException(status_code=404, detail="No hay rutas asignadas para esta patente hoy.")
    return {"estado": "Exito", "patente": patente, "total_paradas": len(ruta), "ruta": ruta}

class ActualizacionEstado(BaseModel):
    id_despacho: int
    nuevo_estado: str 

@app.post("/actualizar-estado")
def actualizar_estado(datos: ActualizacionEstado):
    conexion = psycopg2.connect(URL_BASE_DATOS)
    cursor = conexion.cursor()
    cursor.execute("UPDATE rutas_asignadas SET estado = %s WHERE id_despacho = %s", (datos.nuevo_estado, datos.id_despacho))
    conexion.commit()
    filas_afectadas = cursor.rowcount
    conexion.close()
    if filas_afectadas == 0:
        raise HTTPException(status_code=404, detail="Despacho no encontrado.")
    return {"mensaje": "Estado actualizado"}

# ==========================================
# 2. GESTIÓN DE CLIENTES Y GPS
# ==========================================
class ClienteNuevo(BaseModel):
    nombre: str
    direccion: str

@app.post("/crear-cliente")
def crear_cliente(cliente: ClienteNuevo):
    geolocator = ArcGIS()
    try:
        direccion_completa = f"{cliente.direccion}, Región Metropolitana, Chile"
        location = geolocator.geocode(direccion_completa, timeout=10)
        time.sleep(1)
        
        if location:
            conexion = psycopg2.connect(URL_BASE_DATOS)
            cursor = conexion.cursor()
            cursor.execute("""
                INSERT INTO clientes (nombre, direccion, latitud, longitud)
                VALUES (%s, %s, %s, %s)
            """, (cliente.nombre, cliente.direccion, location.latitude, location.longitude))
            conexion.commit()
            conexion.close()
            return {"exito": True, "ubicacion_exacta": f"{location.latitude}, {location.longitude}"}
        else:
            return {"exito": False, "error": "No se encontró la dirección."}
    except Exception as e:
        return {"exito": False, "error": str(e)}

@app.get("/buscar-cliente/{nombre}")
def buscar_cliente(nombre: str):
    try:
        conexion = psycopg2.connect(URL_BASE_DATOS)
        cursor = conexion.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT nombre, direccion FROM clientes WHERE nombre ILIKE %s ORDER BY id DESC LIMIT 1", (nombre,))
        cliente = cursor.fetchone()
        conexion.close()
        
        if cliente:
            return {"exito": True, "cliente": dict(cliente)}
        return {"exito": False}
    except Exception as e:
        return {"exito": False, "error": str(e)}
    
# ==========================================
# 3. MOTOR DE OPTIMIZACIÓN (CON VENTANAS HORARIAS)
# ==========================================
class PuntoRuta(BaseModel):
    direccion: str
    latitud: float
    longitud: float
    ventana: str 

class PeticionOptimizacion(BaseModel):
    patente: str
    puntos: list[PuntoRuta]

def calcular_distancia(lat1, lon1, lat2, lon2):
    R = 6371.0 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return int(R * c * 1000)

@app.post("/optimizar-ruta")
def optimizar_ruta(datos: PeticionOptimizacion):
    puntos = datos.puntos
    if len(puntos) < 2:
        return {"exito": False, "error": "Se necesitan al menos 2 puntos."}

    distancias = []
    for i in range(len(puntos)):
        fila = []
        for j in range(len(puntos)):
            if i == j:
                fila.append(0)
            else:
                fila.append(calcular_distancia(puntos[i].latitud, puntos[i].longitud, puntos[j].latitud, puntos[j].longitud))
        distancias.append(fila)

    manager = pywrapcp.RoutingIndexManager(len(puntos), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        return distancias[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]
    
    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        distancia_metros = distancias[from_node][to_node]
        tiempo_viaje_min = int(distancia_metros / 500)
        tiempo_servicio = 0 if from_node == 0 else 10 
        return tiempo_viaje_min + tiempo_servicio

    time_callback_index = routing.RegisterTransitCallback(time_callback)
    
    routing.distance_callback = distance_callback
    routing.time_callback = time_callback
    
    routing.AddDimension(
        time_callback_index,
        1440,
        1440,
        False,
        'Time'
    )
    time_dimension = routing.GetDimensionOrDie('Time')

    for i, punto in enumerate(puntos):
        index = manager.NodeToIndex(i)
        if punto.ventana == "Mañana (08:00 - 13:00)":
            time_dimension.CumulVar(index).SetRange(0, 300)
        elif punto.ventana == "Tarde (13:00 - 18:00)":
            time_dimension.CumulVar(index).SetRange(300, 600)
        elif "-" in punto.ventana:
            try:
                partes = punto.ventana.split("-")
                h_ini = int(partes[0].strip().split(":")[0])
                h_fin = int(partes[1].strip().split(":")[0])
                min_ini = max(0, (h_ini - 8) * 60)
                min_fin = max(0, (h_fin - 8) * 60)
                time_dimension.CumulVar(index).SetRange(min_ini, min_fin)
            except:
                time_dimension.CumulVar(index).SetRange(0, 1440)
        else:
            time_dimension.CumulVar(index).SetRange(0, 1440)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

    solucion = routing.SolveWithParameters(search_parameters)

    if not solucion:
        return {"exito": False, "error": "Google OR-Tools no pudo encontrar una ruta que cumpla con esos horarios restrictivos."}

    index = routing.Start(0)
    orden_optimo = []
    paso = 0
    
    while not routing.IsEnd(index):
        nodo = manager.IndexToNode(index)
        orden_optimo.append({
            "orden": paso,
            "direccion": puntos[nodo].direccion,
            "latitud": puntos[nodo].latitud,
            "longitud": puntos[nodo].longitud,
            "tipo": "Inicio (Bodega)" if paso == 0 else "Entrega",
            "estado": "PENDIENTE"
        })
        index = solucion.Value(routing.NextVar(index))
        paso += 1

    try:
        conexion = psycopg2.connect(URL_BASE_DATOS)
        cursor = conexion.cursor()
        cursor.execute("DELETE FROM rutas_asignadas WHERE patente = %s", (datos.patente,))
        
        for p in orden_optimo:
            cursor.execute("""
                INSERT INTO rutas_asignadas (patente, orden, direccion, latitud, longitud, tipo, estado)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (datos.patente, p["orden"], p["direccion"], p["latitud"], p["longitud"], p["tipo"], p["estado"]))
            
        conexion.commit()
        conexion.close()
        return {"exito": True, "mensaje": "Ruta calculada y asignada al vehículo.", "ruta_ordenada": orden_optimo}
        
    except Exception as e:
        return {"exito": False, "error": f"Error guardando en base de datos: {e}"}

@app.post("/escanear-ruta")
async def escanear_ruta(file: UploadFile = File(...)):
    contenido = await file.read()
    
    payload = {
        'apikey': 'helloworld',
        'language': 'spa',
        'isOverlayRequired': False
    }
    
    archivos = {'file': (file.filename, contenido, file.content_type)}
    
    try:
        res = requests.post("https://api.ocr.space/parse/image", files=archivos, data=payload)
        datos = res.json()
        
        if datos.get("IsErroredOnProcessing"):
            return {"exito": False, "error": "La IA no pudo procesar esta imagen."}
            
        texto_crudo = datos["ParsedResults"][0]["ParsedText"]
        
        lineas = texto_crudo.split('\n')
        direcciones_encontradas = []
        
        for linea in lineas:
            l = linea.strip()
            if len(l) > 5 and any(c.isdigit() for c in l):
                direcciones_encontradas.append(l)
                
        return {"exito": True, "direcciones": direcciones_encontradas}
        
    except Exception as e:
        return {"exito": False, "error": str(e)}

# ==========================================
# 4. SISTEMA DE FEEDBACK (NUEVO)
# ==========================================
class FeedbackApp(BaseModel):
    estrellas: int
    comentario: str
    fecha: str

@app.post("/feedback")
def recibir_feedback(feedback: FeedbackApp):
    try:
        # Abrimos la conexión a tu base de datos Supabase
        conexion = psycopg2.connect(URL_BASE_DATOS)
        cursor = conexion.cursor()
        
        # Le ordenamos insertar los datos que llegaron desde el celular
        cursor.execute("""
            INSERT INTO feedback (estrellas, comentario, fecha_app)
            VALUES (%s, %s, %s)
        """, (feedback.estrellas, feedback.comentario, feedback.fecha))
        
        conexion.commit() # Guardamos los cambios
        conexion.close()  # Cerramos la puerta
        
        return {"exito": True, "mensaje": "Feedback guardado exitosamente"}
        
    except Exception as e:
        return {"exito": False, "error": f"Error guardando feedback: {str(e)}"}
# ==========================================
# 5. SISTEMA DE PRUEBA DE ENTREGA (POD)
# ==========================================
SUPABASE_URL = "https://xewyromxoprwvtkqveiw.supabase.co"
SUPABASE_KEY = "PEGA_AQUÍ_TU_CLAVE_ANON_PUBLIC" # <--- ¡Pega tu clave aquí!

@app.post("/entregar-pod")
async def entregar_pod(id_despacho: int = Form(...), file: UploadFile = File(...)):
    try:
        # 1. Leer la foto enviada por el celular
        contenido = await file.read()
        nombre_archivo = f"pod_{id_despacho}_{uuid.uuid4().hex[:8]}.jpg"
        
        # 2. Subir la foto al "bucket" de Supabase
        url_storage = f"{SUPABASE_URL}/storage/v1/object/pods/{nombre_archivo}"
        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": file.content_type
        }
        res = requests.post(url_storage, headers=headers, data=contenido)
        
        if res.status_code >= 400:
            return {"exito": False, "error": "Error al guardar foto en la nube"}
            
        # 3. Generar el link público de la foto
        foto_url = f"{SUPABASE_URL}/storage/v1/object/public/pods/{nombre_archivo}"
        
        # 4. Actualizar la base de datos: Marcar como ENTREGADO y guardar el link
        conexion = psycopg2.connect(URL_BASE_DATOS)
        cursor = conexion.cursor()
        cursor.execute("""
            UPDATE rutas_asignadas 
            SET estado = 'ENTREGADO', foto_url = %s 
            WHERE id_despacho = %s
        """, (foto_url, id_despacho))
        conexion.commit()
        conexion.close()
        
        return {"exito": True, "foto_url": foto_url}
        
    except Exception as e:
        return {"exito": False, "error": str(e)}
# ==========================================
# 6. DASHBOARD Y ESTADÍSTICAS
# ==========================================
@app.get("/estadisticas")
def obtener_estadisticas():
    try:
        conexion = psycopg2.connect(URL_BASE_DATOS)
        cursor = conexion.cursor(cursor_factory=RealDictCursor)

        # 1. Total de entregas completadas
        cursor.execute("SELECT COUNT(*) as total FROM rutas_asignadas WHERE estado = 'ENTREGADO'")
        rutas_completadas = cursor.fetchone()['total']

        # 2. Entregas que tienen foto de respaldo (POD)
        cursor.execute("SELECT COUNT(*) as total FROM rutas_asignadas WHERE estado = 'ENTREGADO' AND foto_url IS NOT NULL")
        entregas_pod = cursor.fetchone()['total']

        conexion.close()

        # 3. Matemática de Negocios
        # Calculamos el % de cumplimiento de fotos
        porcentaje_pod = int((entregas_pod / rutas_completadas * 100)) if rutas_completadas > 0 else 0
        
        # Estimación de IA: Asumimos que la optimización ahorra ~2.5 km y ~15 min por cada entrega completada
        km_ahorrados = int(rutas_completadas * 2.5)
        tiempo_ganado_horas = int((rutas_completadas * 15) / 60)

        return {
            "exito": True,
            "km_ahorrados": str(km_ahorrados),
            "tiempo_ganado": str(tiempo_ganado_horas),
            "porcentaje_pod": str(porcentaje_pod),
            "rutas_completadas": str(rutas_completadas)
        }
        
    except Exception as e:
        return {"exito": False, "error": str(e)}
# ==========================================
# ARRANQUE DEL SERVIDOR
# ==========================================
if __name__ == "__main__":
    import uvicorn
    puerto = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=puerto)
