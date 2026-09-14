#!/usr/bin/env python3
"""Indexado automatico de batches de bitacoras en AirVault.

Cada etapa se corre por separado o todas de corrido. El estado vive en el
manifiesto del trabajo, asi que se puede preparar hoy, subir manana e
indexar despues sin repetir nada.

    python run_airvault.py preparar  --job varias24 --csv "output/.../BITS.CSV" --lote "DP | BITS VARIAS 24"
    python run_airvault.py subir     --job varias24 --pdf "output/.../HP-1848CMP.pdf"
    python run_airvault.py descubrir --job varias24 --esperar
    python run_airvault.py plan      --job varias24
    python run_airvault.py indexar   --job varias24 --revisar
    python run_airvault.py verificar --job varias24
    python run_airvault.py todo      --job varias24 --auto

Modos de ``indexar``:
    --revisar  escribe el reporte y espera aprobacion antes de tocar nada
    --auto     escribe sin detenerse
    sin nada   equivale a un dry run: deja el reporte y no escribe
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from app.utils.portable import ensure_portable_env  # noqa: E402

ensure_portable_env()
os.chdir(_ROOT)

from loguru import logger  # noqa: E402

from app.airvault import manifest as manifiestos  # noqa: E402
from app.airvault.client import ClienteHttp  # noqa: E402
from app.airvault.config import AIRVAULT_FILENAME, AirVaultConfig  # noqa: E402
from app.airvault.discovery import (  # noqa: E402
    LoteAmbiguo,
    LoteNoEncontrado,
    buscar_por_id,
)
from app.airvault.flujo import ErrorDeCorrida, Trabajo, paginas_de_lote  # noqa: E402
from app.airvault.indexer import (  # noqa: E402
    ESPERAS_TRAS_ESCRIBIR, Indexador, verificar_lote,
)
from app.airvault.mapping import FLOTA_CACHE_FILENAME, ResolutorFlota  # noqa: E402
from app.airvault.memoria import BITACORAS_POR_LIBRO  # noqa: E402
from app.airvault.model import EstadoEtapa, Manifiesto  # noqa: E402
from app.airvault.naming import PREFIJO_POR_DEFECTO  # noqa: E402
from app.airvault.report import escribir_csv, escribir_html, resumen_texto  # noqa: E402
from app.airvault.session import (  # noqa: E402
    Credenciales,
    ErrorDeSesion,
    SesionAirVault,
    comprobar_o_renovar,  # noqa: E402
)
from app.airvault.session import abrir_sesion as _abrir_sesion  # noqa: E402

CARPETA_TRABAJOS = Path("output") / "airvault"


def carpeta_job(job: str) -> Path:
    return CARPETA_TRABAJOS / job


# ── argumentos ─────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="airvault",
        description="Indexa en AirVault los batches de bitacoras ya "
        "procesados por BITS - Clasificación de Bitácoras.",
    )
    parser.add_argument("--verbose", action="store_true", help="Logs detallados")
    sub = parser.add_subparsers(dest="etapa", required=True)

    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument(
        "--job", required=True, help="Nombre del trabajo (carpeta en output/airvault)"
    )

    p = sub.add_parser(
        "preparar",
        parents=[comun],
        help="Arma el manifiesto a partir del CSV de la ejecución",
    )
    p.add_argument("--csv", required=True, help="CSV de la ejecución")
    p.add_argument(
        "--lote",
        default=None,
        help="Nombre del batch. Sin esta opcion se arma solo con "
        "el prefijo y la marca de tiempo de la ejecución.",
    )
    p.add_argument(
        "--prefijo",
        default=PREFIJO_POR_DEFECTO,
        help=f"Prefijo del nombre del batch (default: {PREFIJO_POR_DEFECTO})",
    )
    p.add_argument("--doc-type", default=None, help="Tipo de documento a escribir")
    p.add_argument("--audit-status", default=None, help="Audit Status")

    p = sub.add_parser(
        "subir", parents=[comun], help="Sube el PDF del trabajo por Quick Upload"
    )
    p.add_argument("--pdf", required=True, help="PDF de este trabajo que se va a subir")

    p = sub.add_parser(
        "descubrir", parents=[comun], help="Ubica el batch en AirVault por su nombre"
    )
    p.add_argument(
        "--esperar", action="store_true", help="Sondear hasta que el batch aparezca"
    )
    p = sub.add_parser(
        "plan", parents=[comun], help="Dry run: calcula todo y no escribe nada"
    )

    p = sub.add_parser(
        "indexar", parents=[comun], help="Escribe los indices en el batch"
    )
    grupo = p.add_mutually_exclusive_group()
    grupo.add_argument(
        "--revisar", action="store_true", help="Pedir aprobacion antes de escribir"
    )
    grupo.add_argument(
        "--auto", action="store_true", help="Escribir sin detenerse a preguntar"
    )
    p.add_argument(
        "--sobrescribir",
        action="store_true",
        help="Tambien reescribir las paginas ya validadas",
    )
    p.add_argument(
        "--continuar-con-errores",
        action="store_true",
        help="No detenerse en la primera pagina que falle",
    )
    p.add_argument(
        "--completar",
        action="store_true",
        help="Al terminar, dar el batch por terminado en AirVault. Solo lo acepta con todas las paginas en verde.",
    )
    p.add_argument(
        "--permitir-log-distinto",
        action="store_true",
        help="Permitir reemplazar el Log Page Number que AirVault ya leyo. "
        "Usar solo en una prueba controlada.",
    )

    p = sub.add_parser(
        "verificar", parents=[comun], help="Relee el batch y confirma como quedo"
    )

    p = sub.add_parser(
        "todo", parents=[comun], help="Descubrir, planificar, indexar y verificar"
    )
    p.add_argument("--auto", action="store_true", help="Escribir sin pedir aprobacion")
    p.add_argument("--sobrescribir", action="store_true")

    p = sub.add_parser(
        "memoria",
        help="Contrasta con AirVault la memoria de libros y de fechas",
    )
    p.add_argument(
        "--aplicar",
        action="store_true",
        help="Corregir la memoria con lo que diga AirVault. Sin esta "
        "opcion solo se informa de las diferencias.",
    )
    p.add_argument(
        "--libro",
        action="append",
        default=None,
        metavar="CLAVE",
        help="Comprobar solo ese libro (por ejemplo 23159B). Se puede "
        "repetir; sin la opcion se comprueban todos los guardados.",
    )
    p.add_argument(
        "--bitacoras",
        type=int,
        default=BITACORAS_POR_LIBRO,
        help=f"Cuantas bitacoras de cada libro se consultan "
        f"(default: {BITACORAS_POR_LIBRO})",
    )

    for nombre in ("subir", "descubrir", "plan", "indexar", "verificar",
                   "todo", "memoria"):
        sp = _subparser(sub, nombre)
        sp.add_argument(
            "--cookie",
            default=None,
            help="Cookie de sesion ya obtenida en el navegador",
        )
        sp.add_argument(
            "--perfil-edge",
            default=None,
            help="Carpeta del perfil de Edge que usa el programa "
            "para entrar (por defecto, portable/)",
        )
        sp.add_argument(
            "--sin-edge",
            action="store_true",
            help="No abrir el navegador; usar solo la cookie",
        )
        sp.add_argument(
            "--usuario",
            default=None,
            help="Usuario de una cuenta local de AirVault; las "
            "cuentas de Microsoft entran por cookie",
        )
    return parser.parse_args()


def _subparser(sub, nombre: str) -> argparse.ArgumentParser:
    return sub.choices[nombre]


# ── sesion ─────────────────────────────────────────────────────────


def abrir_sesion(config: AirVaultConfig, args) -> SesionAirVault:
    """Abre la sesion con la primera fuente disponible y la comprueba.

    La comprobacion es una peticion de mas al principio que evita el peor
    final posible: descubrir que la cookie habia caducado a mitad de un
    batch de cuatrocientas paginas.
    """
    usuario = getattr(args, "usuario", "") or config.usuario
    credenciales = Credenciales.desde_entorno()
    if credenciales is None and getattr(args, "usuario", ""):
        # Solo se pregunta cuando alguien pide expresamente entrar con una
        # cuenta local: la cuenta federada no tiene formulario que llenar.
        credenciales = Credenciales.preguntar(usuario)
    perfil = getattr(args, "perfil_edge", None)
    sesion = _abrir_sesion(
        config,
        cookie=getattr(args, "cookie", None),
        perfil=Path(perfil) if perfil else None,
        usar_edge=not getattr(args, "sin_edge", False),
        credenciales=credenciales,
        avisar=print,
    )
    lotes = comprobar_o_renovar(sesion, avisar=print)
    print(f"Sesion de AirVault lista ({sesion.origen}); {lotes} batches en la cola")
    return sesion


# ── etapas ─────────────────────────────────────────────────────────


def etapa_preparar(args, config: AirVaultConfig) -> int:
    """Arma el manifiesto del trabajo a partir de la ejecución.

    Pasa por el mismo camino que la ventana para que las dos lean igual el
    indice de paginas del PDF: si la linea de comandos contara solo las
    bitacoras y el PDF llevara separadores, escribiria cada dato una pagina
    mas alla de donde va.
    """
    carpeta = carpeta_job(args.job)
    resolutor = ResolutorFlota.load(_ROOT / FLOTA_CACHE_FILENAME)
    try:
        trabajo = Trabajo.preparar(
            config,
            carpeta,
            args.csv,
            nombre_lote=args.lote or "",
            prefijo=getattr(args, "prefijo", PREFIJO_POR_DEFECTO),
            resolutor=resolutor,
        )
    except ErrorDeCorrida as exc:
        print(str(exc), file=sys.stderr)
        return 1
    manifiesto = trabajo.manifiesto
    if args.doc_type:
        manifiesto.doc_type = args.doc_type
    if args.audit_status:
        manifiesto.audit_status = args.audit_status
    trabajo.guardar()

    bitacoras = manifiesto.bitacoras()
    separadores = manifiesto.separadores()
    inferidas = sum(1 for r in bitacoras if r.fleet_inferido)
    print(f"Manifiesto creado en {manifiestos.ruta_manifiesto(carpeta)}")
    print(f"  bitacoras: {len(bitacoras)}")
    if separadores:
        print(
            f"  separadores del PDF: {len(separadores)} "
            f"(ocupan pagina en el batch y no se indexan)"
        )
    print(f"  batch:      {manifiesto.nombre_batch}")
    print("  el batch debe subirse a AirVault con ese mismo nombre")
    if inferidas:
        print(
            f"  flota inferida por regla en {inferidas} bitacoras "
            f"(revisar en el reporte)"
        )
    return 0


def etapa_subir(args, config: AirVaultConfig) -> int:
    carpeta = carpeta_job(args.job)
    manifiesto = manifiestos.cargar(carpeta)
    sesion = abrir_sesion(config, args)
    cliente = ClienteHttp(sesion, config)
    trabajo = Trabajo(config, carpeta, manifiesto)
    archivo = Path(args.pdf)
    trabajo.subir(sesion, archivo, cliente=cliente)
    print(f"  {archivo.name}: ok")
    return 0


def etapa_descubrir(
    args, config: AirVaultConfig, manifiesto: Manifiesto | None = None
) -> int:
    carpeta = carpeta_job(args.job)
    manifiesto = manifiesto or manifiestos.cargar(carpeta)
    sesion = abrir_sesion(config, args)
    cliente = ClienteHttp(sesion, config)
    esperadas = len(manifiesto.registros)
    # Lo hace el mismo recorrido que usa la ventana: primero confirma el
    # nombre enviado y, si AirVault lo perdio, identifica el Empty-Batch por
    # cantidad de paginas y contenido antes de renombrarlo.
    trabajo = Trabajo(config, carpeta, manifiesto)
    try:
        trabajo.descubrir(cliente, esperar=getattr(args, "esperar", False))
    except (LoteNoEncontrado, LoteAmbiguo) as exc:
        manifiesto.etapa("descubrir").marcar(EstadoEtapa.ERROR, str(exc))
        manifiestos.guardar(manifiesto, carpeta)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    lote = buscar_por_id(cliente.listar_lotes(), manifiesto.batch_id)
    paginas = lote.paginas if lote else esperadas
    print(
        f"Batch encontrado: {manifiesto.batch_id} - "
        f"{lote.nombre if lote else manifiesto.nombre_batch} "
        f"({paginas} paginas)"
    )
    if paginas != esperadas:
        print(
            f"AVISO: el batch tiene {paginas} paginas y el manifiesto "
            f"{esperadas}. El indexado no va a escribir hasta que "
            f"coincidan."
        )
    return 0


def _planificar(args, config: AirVaultConfig, sobrescribir: bool = False):
    carpeta = carpeta_job(args.job)
    manifiesto = manifiestos.cargar(carpeta)
    if not manifiesto.batch_id:
        raise SystemExit(
            "El trabajo todavía no tiene batch. Correr 'descubrir' primero."
        )
    sesion = abrir_sesion(config, args)
    cliente = ClienteHttp(sesion, config)
    info = cliente.abrir_lote(manifiesto.batch_id)
    paginas = paginas_de_lote(info)
    try:
        picklist = cliente.picklist_matriculas()
    except Exception as exc:  # noqa: BLE001 - el catalogo no es critico
        logger.warning("No se pudo leer el picklist de matriculas: {}", exc)
        picklist = []
    resolutor = ResolutorFlota.load(_ROOT / FLOTA_CACHE_FILENAME)
    indexador = Indexador(
        cliente,
        manifiesto,
        picklist,
        sobrescribir,
        al_guardar=lambda m: manifiestos.guardar(m, carpeta),
        resolutor=resolutor,
        permitir_log_distinto=getattr(args, "permitir_log_distinto", False),
    )
    try:
        plan = indexador.planificar(paginas)
    except BaseException:
        # Sin plan no se escribe nada, y un batch que queda tomado deja
        # colgada la siguiente apertura sin decir por que.
        _soltar(cliente, manifiesto.batch_id)
        raise
    # Lo aprendido del batch sirve para los siguientes: se guarda siempre,
    # tambien en dry run, porque no toca nada de AirVault.
    resolutor.guardar(_ROOT / FLOTA_CACHE_FILENAME)
    _comprobar_memoria(indexador)
    manifiestos.guardar(manifiesto, carpeta)
    escribir_csv(plan, carpeta / "revision.csv")
    escribir_html(
        plan,
        carpeta / "revision.html",
        f"{manifiesto.nombre_batch} ({manifiesto.batch_id})",
    )
    return manifiesto, indexador, plan, carpeta, cliente


def _comprobar_memoria(indexador) -> None:
    """Contrasta la memoria de libros con las paginas que ya se leyeron.

    Va aqui por lo mismo que el cache de flota: sale de la misma lectura
    del batch, no toca nada de AirVault y sirve para las ejecuciones
    siguientes, asi que se hace igual en el dry run que en el indexado. Un
    fallo no puede tumbar un plan: la memoria es una ayuda, no el trabajo.
    """
    from app.airvault.flujo import comprobar_memoria_de_libros

    informe = comprobar_memoria_de_libros(indexador, _ROOT)
    if informe is not None and (informe.hay_cambios or informe.conflictos):
        print(_detalle_memoria(informe))


def _detalle_memoria(informe) -> str:
    """El informe de la memoria en texto, sin repetir lo que ya coincidia."""
    from app.validation.book_memory import CONFIRMADO

    lineas = [informe.resumen()]
    lineas.extend(
        f"  {cambio}" for cambio in informe.cambios
        if cambio.accion != CONFIRMADO
    )
    return "\n".join(lineas)


def _soltar(cliente, batch_id: str) -> None:
    """Suelta el batch en AirVault; nunca levanta, es limpieza.

    `LockAndGetBatchInfo` deja el batch tomado a nombre de quien lo abrio y
    AirVault solo admite un dueno: si no se suelta, la proxima apertura
    (la del programa o la de la persona que entra por el navegador) se
    queda esperando sin contestar.
    """
    if not batch_id:
        return
    try:
        cliente.cerrar_lote(batch_id)
    except Exception as exc:  # noqa: BLE001 - cerrar nunca tumba nada
        logger.warning(
            "No se pudo soltar el batch {}: {}. Si la siguiente apertura se "
            "queda esperando, hay que cerrarlo en AirVault a mano.",
            batch_id,
            exc,
        )


def etapa_plan(args, config: AirVaultConfig) -> int:
    manifiesto, _indexador, plan, carpeta, cliente = _planificar(args, config)
    # El plan solo lee, asi que el batch se suelta en cuanto termina:
    # dejarlo tomado cuelga la siguiente apertura, la del programa o la
    # de quien lo abra en el navegador.
    _soltar(cliente, manifiesto.batch_id)
    print(resumen_texto(plan))
    print(f"\nReporte: {carpeta / 'revision.html'}")
    print("Nada fue escrito. Para escribir: indexar --revisar o --auto")
    return 0


def etapa_indexar(args, config: AirVaultConfig) -> int:
    manifiesto, indexador, plan, carpeta, cliente = _planificar(
        args, config, getattr(args, "sobrescribir", False)
    )
    print(resumen_texto(plan))
    print(f"\nReporte: {carpeta / 'revision.html'}")

    try:
        if not (args.revisar or args.auto):
            print("Dry run: nada fue escrito.")
            return 0
        if args.revisar:
            respuesta = (
                input(
                    f"\nEscribir {len(plan.escribibles)} paginas en "
                    f"{plan.batch_id}? [escribir/no]: "
                )
                .strip()
                .lower()
            )
            if respuesta != "escribir":
                print("Cancelado. Nada fue escrito.")
                return 0

        manifiesto.etapa("indexar").marcar(EstadoEtapa.EN_CURSO)
        manifiestos.guardar(manifiesto, carpeta)
        resultado = indexador.aplicar(
            plan,
            detener_en_error=not getattr(args, "continuar_con_errores", False),
        )
    finally:
        # Salga como salga (cancelado, a medias o completo) el batch se
        # suelta: AirVault admite un solo dueno y el que queda tomado
        # cuelga la siguiente apertura sin decir por que.
        _soltar(cliente, manifiesto.batch_id)
    validas, total, problemas = verificar_lote(
        cliente, manifiesto, esperas=ESPERAS_TRAS_ESCRIBIR,
    )
    incompleto = not manifiesto.solo_subir and validas != total
    hubo_error = bool(
        resultado.fallidas
        or resultado.separadores_pendientes
        or resultado.interrumpido
        or incompleto
    )
    estado = EstadoEtapa.ERROR if hubo_error else EstadoEtapa.HECHA
    manifiesto.etapa("indexar").marcar(
        estado,
        f"escritas {resultado.escritas}, omitidas {resultado.omitidas}, "
        f"fallidas {resultado.fallidas}; {validas}/{total} en Valid",
    )
    manifiesto.etapa("verificar").marcar(
        EstadoEtapa.HECHA if validas == total else EstadoEtapa.ERROR,
        f"{validas}/{total} en Valid",
    )
    manifiestos.guardar(manifiesto, carpeta)
    print(f"\nEscritas:  {resultado.escritas}")
    print(f"Omitidas:  {resultado.omitidas}")
    print(f"Fallidas:  {resultado.fallidas}")
    print(f"En verde:  {validas}/{total}")
    for detalle in resultado.detalles[:10]:
        print(f"  {detalle}")
    for problema in problemas[:10]:
        print(f"  {problema}")
    if getattr(args, "completar", False):
        _completar(carpeta, manifiesto, cliente, config)
    return 1 if hubo_error else 0


def _completar(
    carpeta: Path, manifiesto: Manifiesto, cliente, config: AirVaultConfig
) -> None:
    """Da el batch por terminado, si AirVault lo va a aceptar.

    Solo cierra un batch con todas las paginas en verde: basta una a
    la que le falte un campo obligatorio (casi siempre la fecha)
    para que lo rechace. Asi que se mira antes y, si alguna bloquea,
    se dice cual y el batch se queda en la cola, que es justo donde
    tiene que quedarse.
    """
    resultado = Trabajo(config, carpeta, manifiesto).completar(cliente)
    print()
    if resultado.completado:
        print(f"Batch {manifiesto.batch_id} cerrado en AirVault ({resultado.detalle}).")
        return
    print(f"El batch {manifiesto.batch_id} se queda en la cola: {resultado.detalle}")


def etapa_verificar(args, config: AirVaultConfig) -> int:
    carpeta = carpeta_job(args.job)
    manifiesto = manifiestos.cargar(carpeta)
    if not manifiesto.batch_id:
        print("El trabajo no tiene batch asignado", file=sys.stderr)
        return 1
    sesion = abrir_sesion(config, args)
    cliente = ClienteHttp(sesion, config)
    validas, total, problemas = verificar_lote(cliente, manifiesto)
    manifiesto.etapa("verificar").marcar(
        EstadoEtapa.HECHA if validas == total else EstadoEtapa.ERROR,
        f"{validas}/{total} en Valid",
    )
    manifiestos.guardar(manifiesto, carpeta)
    print(f"Paginas en Valid: {validas} de {total}")
    for problema in problemas[:20]:
        print(f"  {problema}")
    if len(problemas) > 20:
        print(f"  ... y {len(problemas) - 20} mas")
    return 0 if validas == total else 1


def etapa_todo(args, config: AirVaultConfig) -> int:
    args.esperar = True
    codigo = etapa_descubrir(args, config)
    if codigo:
        return codigo
    args.revisar = not args.auto
    args.continuar_con_errores = False
    codigo = etapa_indexar(args, config)
    if codigo:
        return codigo
    return etapa_verificar(args, config)


def etapa_memoria(args, config: AirVaultConfig) -> int:
    """Contrasta con Web Search la memoria de libros y de fechas.

    Alcanza a los libros que no vienen en ningun batch de hoy, que son casi
    todos, y a los que se publicaron sin este programa, que son los unicos
    en que AirVault es de verdad una fuente externa. Nadie tiene que estar
    delante: informa por consola y solo escribe con ``--aplicar``.
    """
    from app.airvault import duplicados as libro_de_envios
    from app.airvault.memoria import (
        libros_de_la_memoria,
        verificar_con_websearch,
    )
    from app.airvault.websearch import Buscador

    claves = list(args.libro) if args.libro else libros_de_la_memoria(_ROOT)
    if not claves:
        print("La memoria de libros esta vacia: no hay nada que comprobar.")
        return 0
    sesion = abrir_sesion(config, args)
    buscador = Buscador(
        sesion=sesion,
        config=config,
        ruta_config=_ROOT / AIRVAULT_FILENAME,
        controles=libro_de_envios.leer(CARPETA_TRABAJOS).controles(),
    )
    print(f"Comprobando {len(claves)} libro(s) en Web Search...")

    def avanzar(hechos: int, total: int) -> None:
        print(f"  {hechos}/{total}", end="\r", flush=True)

    informe = verificar_con_websearch(
        buscador, _ROOT, claves,
        cuantas=max(1, int(args.bitacoras)),
        escribir=bool(args.aplicar),
        al_avanzar=avanzar,
    )
    print(" " * 20, end="\r")
    print(_detalle_memoria(informe))
    if not informe.observaciones:
        print(
            "Web Search no devolvio ninguna de las bitacoras consultadas. "
            "Puede que esos libros no esten publicados o que la consulta no "
            "sea la que se probo: no se cambia nada."
        )
        return 1
    if informe.hay_cambios and not args.aplicar:
        print("Nada fue escrito. Para corregir la memoria: memoria --aplicar")
    return 0


ETAPAS = {
    "preparar": etapa_preparar,
    "subir": etapa_subir,
    "descubrir": etapa_descubrir,
    "plan": etapa_plan,
    "indexar": etapa_indexar,
    "verificar": etapa_verificar,
    "todo": etapa_todo,
    "memoria": etapa_memoria,
}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO")
    config = AirVaultConfig.load(_ROOT / AIRVAULT_FILENAME)
    try:
        return ETAPAS[args.etapa](args, config)
    except (ErrorDeSesion, FileNotFoundError, ValueError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI amigable
        print(f"\nERROR: {exc}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
