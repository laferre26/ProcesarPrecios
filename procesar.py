"""Busca el último CSV "Recomendador_La Ferre" que se subió al SFTP de Pricelabs,
lo carga en LFActualiza, aplica los precios en ListaPreciosD, sube el archivo de
INSERT a la carpeta Procesado y avisa por correo.

Pricelabs a veces vuelve a subir un archivo con el mismo nombre y datos distintos, por eso
se compara el contenido contra lo que hay en Procesado y no solo el nombre.

En la BD solo se tocan los artículos/listas que vienen en el archivo, todo en una transacción:
  - LFActualiza: se borra el renglón anterior y se inserta el nuevo (respaldo).
  - ListaPreciosD: desde LFActualiza se actualiza Precio (PrecioMinimo no se toca) y se
    registra el cambio en ArtPrecioHist. Si el artículo no estaba en la lista se da de alta
    con PrecioMinimo = 96% del precio, igual que xpActualizarPrecioLista.
    No se aplican variaciones mayores a ±60% ni precios de listas/artículos que no existen;
    esos renglones se reportan en el correo.

Si la corrida del timer falla se avisa por correo una sola vez, y otra vez cuando se recupera.
El aviso va solo a `alertas` de la sección [correo].

Uso:
    procesar.py              # se conecta al SFTP y a la BD vía bridge-api (lo que corre el timer)
    procesar.py --forzar     # vuelve a procesar el más reciente aunque no haya cambiado
    procesar.py --simular    # muestra qué cambiaría en la BD sin guardar nada ni enviar correo
    procesar.py --local X    # procesa el archivo local X e imprime el resultado
"""

import argparse
import configparser
import csv
import html
import io
import logging
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

CONFIG = Path.home() / ".config" / "pricelabs-sftp" / "config.ini"
ALERTA = CONFIG.with_name("alerta_enviada")  # existe mientras haya una falla ya avisada

PATRON = re.compile(r"^Recomendador_La Ferre.*\.csv$")

VARIACION_MAXIMA = 60  # %, mismo límite que xpActualizarPrecioLista

log = logging.getLogger("pricelabs")


def nombre_salida(nombre):
    return nombre.replace("Recomendador_La Ferre", "LFActualiza", 1)


def leer_remoto(sftp, ruta):
    """Contenido del archivo remoto, o None si no existe."""
    try:
        with sftp.open(ruta, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def sql_texto(valor):
    return "'" + valor.strip().replace("'", "''") + "'"


def leer_filas(texto_csv):
    """Lista de (codigo, precio, lista); si un artículo/lista se repite gana el último."""
    filas = {}
    for num, fila in enumerate(csv.DictReader(io.StringIO(texto_csv)), start=2):
        try:
            codigo = fila["Cod_articulo"].strip()
            lista = fila["Lista_base"].strip()
            precio = Decimal(fila["Precio"].strip())
        except (KeyError, AttributeError, InvalidOperation):
            log.warning("Fila %d inválida, se omite: %s", num, fila)
            continue
        filas[(codigo, lista)] = (codigo, precio, lista)
    return list(filas.values())


def generar_inserts(filas):
    lineas = [
        f"INSERT into LFActualiza Select {sql_texto(codigo)},{precio},{sql_texto(lista)}"
        for codigo, precio, lista in filas
    ]
    return "\n".join(lineas) + "\n"


def armar_lote(filas, guardar=True):
    """Un solo lote T-SQL: carga LFActualiza y desde ahí aplica en ListaPreciosD.

    Devuelve un renglón por artículo/lista con el precio anterior, el nuevo y el estado.
    Con guardar=False hace todo y al final un ROLLBACK, para simular.
    """
    valores = [f"({sql_texto(codigo)},{precio},{sql_texto(lista)})" for codigo, precio, lista in filas]
    # SQL Server acepta máximo 1000 renglones por VALUES
    inserts = "\n".join(
        "INSERT INTO @nuevos (SKU, precio, list) VALUES\n" + ",\n".join(valores[i:i + 1000]) + ";"
        for i in range(0, len(valores), 1000)
    )
    return f"""SET XACT_ABORT ON;
SET NOCOUNT ON;
BEGIN TRAN;
DECLARE @nuevos TABLE (SKU varchar(20), precio float, list varchar(50));
{inserts}
DELETE a FROM LaFerre.dbo.LFActualiza a JOIN @nuevos n ON a.SKU = n.SKU AND a.list = n.list;
INSERT INTO LaFerre.dbo.LFActualiza (SKU, precio, list) SELECT SKU, precio, list FROM @nuevos;

DECLARE @res TABLE (SKU varchar(20), list varchar(50), ListaId int, Lista varchar(20),
                    anterior money, nuevo money, estado varchar(20));
INSERT INTO @res
SELECT f.SKU, f.list, lp.ID, lp.Lista, ld.Precio, CAST(f.precio AS money),
       CASE WHEN lp.Lista IS NULL THEN 'Lista no existe'
            WHEN a.Articulo IS NULL THEN 'Artículo no existe'
            WHEN a.Estatus <> 'Alta' THEN 'Artículo no activo'
            WHEN ld.Articulo IS NULL THEN 'Nuevo'
            WHEN ROUND(ld.Precio, 2) = ROUND(CAST(f.precio AS money), 2) THEN 'Sin cambio'
            WHEN ISNULL(ld.Precio, 0) = 0
                 OR ABS(f.precio - ld.Precio) * 100.0 / ld.Precio > {VARIACION_MAXIMA} THEN 'Fuera de rango'
            ELSE 'Actualizado' END
FROM LaFerre.dbo.LFActualiza f
JOIN @nuevos n ON n.SKU = f.SKU AND n.list = f.list
LEFT JOIN LaFerre.dbo.ListaPrecios lp ON lp.Lista = f.list AND lp.Moneda = 'Pesos'
LEFT JOIN LaFerre.dbo.Art a ON a.Articulo = f.SKU
LEFT JOIN LaFerre.dbo.ListaPreciosD ld ON ld.Lista = lp.Lista AND ld.Moneda = 'Pesos' AND ld.Articulo = f.SKU;

DECLARE @fecha datetime = GETDATE();
UPDATE ld SET Precio = r.nuevo
FROM LaFerre.dbo.ListaPreciosD ld
JOIN @res r ON ld.Lista = r.Lista AND ld.Moneda = 'Pesos' AND ld.Articulo = r.SKU
WHERE r.estado = 'Actualizado';
INSERT INTO LaFerre.dbo.ListaPreciosD (Lista, Moneda, Articulo, Precio, PrecioMinimo)
SELECT Lista, 'Pesos', SKU, nuevo, nuevo * 0.96 FROM @res WHERE estado = 'Nuevo';
INSERT INTO LaFerre.dbo.ArtPrecioHist (Articulo, Numero, Fecha, PrecioAnterior, PrecioActual)
SELECT SKU, ListaId, @fecha, ISNULL(anterior, 0), nuevo FROM @res WHERE estado IN ('Actualizado', 'Nuevo')
UNION ALL
SELECT SKU, 9000 + ListaId, @fecha, 0, nuevo * 0.96 FROM @res WHERE estado = 'Nuevo';
{"COMMIT" if guardar else "ROLLBACK"};

SELECT SKU, list, anterior, nuevo, estado FROM @res ORDER BY estado, SKU, list;
"""


def aplicar_en_bd(cfg, filas, guardar=True):
    """Manda el lote al bridge-api de la intranet, que lo ejecuta desde la red de la BD."""
    import json
    import urllib.request

    peticion = urllib.request.Request(
        cfg["url"].rstrip("/") + "/execute",
        data=json.dumps({"query": armar_lote(filas, guardar)}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-bridge-token": cfg["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=120) as r:
            respuesta = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"bridge-api respondió {e.code}: {e.read().decode(errors='replace')}")
    resultado = respuesta["recordset"]
    if len(resultado) != len(filas):
        raise RuntimeError(f"Se esperaban {len(filas)} renglones y el bridge devolvió {len(resultado)}")
    return resultado


def resumen(resultado):
    return ", ".join(f"{estado}: {n}" for estado, n in sorted(Counter(r["estado"] for r in resultado).items()))


def armar_correo(archivo, resultado):
    conteo = Counter(r["estado"] for r in resultado)
    aplicados = [r for r in resultado if r["estado"] in ("Actualizado", "Nuevo")]
    revisar = [r for r in resultado if r["estado"] not in ("Actualizado", "Nuevo", "Sin cambio")]

    def precio(valor):
        return "" if valor is None else f"${valor:,.2f}"

    def tabla(renglones):
        cuerpo = "".join(
            f"<tr><td>{html.escape(r['SKU'])}</td><td>{html.escape(r['list'])}</td>"
            f"<td align='right'>{precio(r['anterior'])}</td><td align='right'>{precio(r['nuevo'])}</td>"
            f"<td>{html.escape(r['estado'])}</td></tr>"
            for r in renglones
        )
        return ("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse'>"
                "<tr style='background:#f3f4f6'><th>Artículo</th><th>Lista</th><th>Precio anterior</th>"
                f"<th>Precio nuevo</th><th>Estado</th></tr>{cuerpo}</table>")

    partes = [
        "<html><body style='font-family:Arial,sans-serif;color:#333'>",
        "<h2>Lista de Precios Actualizada</h2>",
        f"<p>Se aplicó el archivo <strong>{html.escape(archivo)}</strong> de Pricelabs.</p>",
        "<ul>",
        f"<li>Precios actualizados: <strong>{conteo['Actualizado']}</strong></li>",
        f"<li>Altas en lista: <strong>{conteo['Nuevo']}</strong></li>",
        f"<li>Sin cambio: {conteo['Sin cambio']}</li>",
        f"<li>No aplicados (revisar): <strong>{len(revisar)}</strong></li>",
        "</ul>",
    ]
    if revisar:
        partes += [f"<h3>No aplicados</h3><p>Variación mayor a ±{VARIACION_MAXIMA}% o lista/artículo inexistente.</p>",
                   tabla(revisar)]
    if aplicados:
        partes += ["<h3>Precios aplicados</h3>", tabla(aplicados)]
    partes.append("</body></html>")
    return "".join(partes)


def enviar_correo(cfg, asunto, cuerpo_html, para=None):
    import smtplib
    from email.message import EmailMessage

    para = [d.strip() for d in (para or cfg["para"]).split(",") if d.strip()]
    mensaje = EmailMessage()
    mensaje["Subject"] = asunto
    mensaje["From"] = cfg.get("remitente") or cfg["user"]
    mensaje["To"] = ", ".join(para)
    mensaje.set_content(f"{asunto}. Abre este correo en un cliente que muestre HTML.")
    mensaje.add_alternative(cuerpo_html, subtype="html")

    with smtplib.SMTP(cfg.get("host", "smtp.office365.com"), cfg.getint("port", 587), timeout=60) as smtp:
        smtp.starttls()
        # Se aceptan comillas alrededor, como vienen en el .env de la intranet
        smtp.login(cfg["user"].strip("\"'"), cfg["password"].strip("\"'"))
        smtp.send_message(mensaje)


def cargar_config():
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(CONFIG):
        sys.exit(f"Falta el archivo de configuración: {CONFIG}")
    for seccion in ("sftp", "bridge", "correo"):
        if not cfg.has_section(seccion):
            sys.exit(f"Falta la sección [{seccion}] en {CONFIG}")
    return cfg


def conectar(cfg):
    import paramiko

    host, port, user = cfg["host"], cfg.getint("port", 22), cfg["user"]
    password = cfg.get("password") or None
    key_file = cfg.get("key_file") or None

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    if key_file:
        client.connect(host, port=port, username=user,
                       key_filename=str(Path(key_file).expanduser()), timeout=30)
        return client

    # El servidor no acepta el método "password", solo "keyboard-interactive":
    # se responde la contraseña a cada pregunta que haga el servidor
    import socket

    sock = socket.create_connection((host, port), timeout=30)
    transport = paramiko.Transport(sock)
    transport.start_client(timeout=30)
    remota = transport.get_remote_server_key()
    conocida = paramiko.HostKeys(str(Path.home() / ".ssh" / "known_hosts")).lookup(host)
    if not conocida or conocida.get(remota.get_name()) != remota:
        transport.close()
        raise paramiko.SSHException(f"Llave del servidor {host} desconocida o distinta")
    transport.auth_interactive(user, lambda titulo, instr, preguntas: [password] * len(preguntas))
    client._transport = transport
    return client


def ejecutar_sftp(forzar=False, simular=False):
    config = cargar_config()
    cfg = config["sftp"]
    origen = cfg["origen"].rstrip("/")
    destino = cfg["destino"].rstrip("/")

    client = conectar(cfg)
    try:
        sftp = client.open_sftp()

        candidatos = [a for a in sftp.listdir_attr(origen) if PATRON.match(a.filename)]
        if not candidatos:
            log.info("No hay archivos Recomendador en %s", origen)
            return
        reciente = max(candidatos, key=lambda a: a.st_mtime).filename
        salida = f"{destino}/{nombre_salida(reciente)}"

        filas = leer_filas(leer_remoto(sftp, f"{origen}/{reciente}").decode("utf-8-sig"))
        resultado_inserts = generar_inserts(filas)

        if not (forzar or simular) and leer_remoto(sftp, salida) == resultado_inserts.encode("utf-8"):
            log.info("%s ya fue procesado (%s es igual), nada que hacer", reciente, salida)
            return

        if simular:
            resultado = aplicar_en_bd(config["bridge"], filas, guardar=False)
            log.info("SIMULACIÓN de %s (no se guardó nada): %s", reciente, resumen(resultado))
            for r in resultado:
                if r["estado"] != "Sin cambio":
                    print(f"{r['estado']:<20} {r['SKU']:<20} {r['list']:<22} {r['anterior']} -> {r['nuevo']}")
            return

        # Primero la BD: si falla no se sube el archivo y el siguiente ciclo lo reintenta.
        # Reaplicar es seguro porque LFActualiza se reemplaza por artículo/lista y en
        # ListaPreciosD los precios que ya quedaron iguales salen como "Sin cambio".
        resultado = aplicar_en_bd(config["bridge"], filas)
        log.info("BD actualizada con %s: %s", reciente, resumen(resultado))

        # Se sube con nombre temporal y luego se renombra, para que nadie lea un archivo a medias
        temporal = salida + ".tmp"
        sftp.putfo(io.BytesIO(resultado_inserts.encode("utf-8")), temporal)
        sftp.posix_rename(temporal, salida)
        log.info("Procesado %s -> %s (%d inserts)", reciente, salida, resultado_inserts.count("\n"))
    finally:
        client.close()

    # El correo va al final: si falla, los precios ya quedaron aplicados y no se reintenta
    enviar_correo(config["correo"], "Lista de Precios Actualizada", armar_correo(reciente, resultado))
    log.info("Correo enviado a %s", config["correo"]["para"])


def avisar(asunto, cuerpo_html):
    """Correo de estado del proceso: va solo a `alertas` de [correo], nunca a `para`."""
    correo = cargar_config()["correo"]
    enviar_correo(correo, asunto, cuerpo_html, correo["alertas"])


def ejecutar_con_alerta():
    """Corrida del timer: avisa al primer fallo y al recuperarse, no en cada ciclo."""
    import socket

    equipo = socket.gethostname()
    try:
        ejecutar_sftp()
    except Exception as e:
        log.exception("Falló el proceso")
        if not ALERTA.exists():
            detalle = html.escape(f"{type(e).__name__}: {e}"[:2000])
            try:
                avisar(
                    "ALERTA: falló la actualización de precios de Pricelabs",
                    "<html><body style='font-family:Arial,sans-serif;color:#333'>"
                    "<h2>Falló la actualización de precios</h2>"
                    f"<p>Equipo: <strong>{html.escape(equipo)}</strong></p><pre>{detalle}</pre>"
                    "<p>El proceso sigue reintentando cada 15 minutos. Revisa si los precios ya se "
                    "aplicaron antes de reprocesar. Detalle en el servidor:</p>"
                    "<pre>journalctl -u pricelabs-sftp.service -n 50 --no-pager</pre>"
                    "</body></html>",
                )
                ALERTA.touch()
            except Exception:
                # Sin marca: el siguiente ciclo vuelve a intentar la alerta
                log.exception("No se pudo enviar la alerta")
        sys.exit(1)

    if ALERTA.exists():
        avisar(
            "Resuelto: la actualización de precios de Pricelabs volvió a funcionar",
            "<html><body style='font-family:Arial,sans-serif;color:#333'>"
            f"<p>El proceso en <strong>{html.escape(equipo)}</strong> volvió a correr sin errores.</p>"
            "</body></html>",
        )
        ALERTA.unlink()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", type=Path, help="procesar un CSV local e imprimir el resultado")
    parser.add_argument("--forzar", action="store_true",
                        help="reprocesar el más reciente aunque no haya cambiado")
    parser.add_argument("--simular", action="store_true",
                        help="mostrar qué cambiaría en la BD sin guardar nada ni enviar correo")
    args = parser.parse_args()

    if args.local:
        sys.stdout.write(generar_inserts(leer_filas(args.local.read_text(encoding="utf-8-sig"))))
    elif args.forzar or args.simular:
        ejecutar_sftp(args.forzar, args.simular)
    else:
        ejecutar_con_alerta()


if __name__ == "__main__":
    main()
