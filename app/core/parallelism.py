"""Cálculo de la configuración de paralelismo del OCR (solo CPU).

Medido sobre la carga OCR real del pipeline (PP-OCRv6_medium_det +
PP-OCRv5_mobile_rec, PaddlePaddle CPU, Ryzen 5 3600), en páginas por
segundo. Las máquinas más pequeñas se simulan confinando la afinidad de CPU
(los procesos hijos heredan la máscara), con las parejas SMT verificadas:
los lógicos 0 y 1 son el mismo núcleo físico (dos procesos ahí rinden 0.105;
repartidos en dos núcleos, 0.167).

    equipo              procesos x hilos    páginas/s
    2 núcleos / 4 hilos     4 x 1             0.221   <- mejor
                            2 x 1             0.169
                            2 x 2             0.158
    4 núcleos / 8 hilos     8 x 1             0.431   <- mejor
                            4 x 1             0.321
                            4 x 2             0.310
    6 núcleos / 12 hilos   12 x 1             0.516   <- mejor
                            8 x 2             0.494
                            8 x 1             0.460
                            6 x 1             0.441
                            6 x 2             0.430
                            6 x 3             0.376
                            4 x 2             0.339
                            4 x 1             0.331
                            4 x 3             0.317
                            3 x 2             0.285
                            3 x 4             0.261
                            3 x 1             0.242
                            2 x 3             0.209
                            2 x 2             0.205
                            2 x 6             0.197
                            2 x 1             0.172
                            1 x 12            0.119
                            1 x 3             0.116
                            1 x 1             0.091

La regla es la misma en los tres equipos: **un proceso por hilo lógico, con
un solo hilo interno**. A igualdad de concurrencia total los procesos ganan
por 20-40% (4x1 frente a 2x2, 8x1 frente a 4x2, 12x1 frente a 6x2), y parar
en los núcleos físicos deja mucho sobre la mesa: 12x1 rinde 17% más que 6x1,
y en el equipo de 4 núcleos 8x1 rinde 34% más que 4x1. El SMT sí aporta en
esta carga, al contrario de lo que suponía la versión anterior.

Los hilos internos aceleran un proceso aislado, pero poco y con techo bajo:
1.31x, ya saturado en 3 hilos (782 -> 592 ms por recorte; 6 y 12 hilos miden
igual que 3). Por eso solo se reparten cuando sobran hilos del presupuesto y
nunca pasan de ``_MAX_ENGINE_THREADS``.

Paralelismo con hilos de Python (``ThreadPoolExecutor`` repartiendo páginas
en un solo proceso) está descartado por medición, no por teoría: 6 hilos
rinden 0.090 páginas/s, exactamente lo mismo que un solo hilo (0.091). El
GIL no se suelta durante la inferencia, así que solo los procesos escalan.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

# Tope duro de procesos. No es el punto donde la curva se aplana (medido,
# sigue subiendo hasta un proceso por hilo lógico) sino un seguro contra
# equipos con muchísimos hilos, donde cada proceso cuesta una copia de los
# modelos y el límite real pasa a ser la memoria. Se fija por encima de los
# equipos previstos (20 hilos) para que quien decida sea la memoria, medida
# en la máquina que ejecuta, y no una constante escrita en otra.
_MAX_WORKERS = 32
# Techo de hilos internos por motor: la inferencia satura en 3 (782 -> 592 ms
# por recorte; con 6 y 12 hilos mide lo mismo que con 3).
_MAX_ENGINE_THREADS = 3
# Memoria residente por proceso worker, medida durante una ejecución real y no
# en un proceso aislado: los picos por proceso llegan a 842-898 MB y la media
# ronda 815-823 MB (0001.pdf y test2.pdf, 8 y 12 procesos). Desglose de lo que
# lo compone: 77 MB de intérprete con numpy y OpenCV, +329 MB al cargar el
# reconocedor, +107 MB al cargar además el detector (515 MB de modelos, que
# son el suelo mientras cada proceso necesite su propia copia) y el resto son
# búferes de página y el arena de Paddle.
#
# Presupuestar 850 MB, menos que el pico ya medido, dejó una ejecución con
# 12.999 MB libres en 11 procesos: después de la reserva quedaban solo 49 MB
# sobre los picos del pool. Windows empezó a paginar los modelos y 22 páginas
# tardaron 517 s, frente a 11 páginas en 60 s sin esa presión. Un GiB por
# proceso cubre el pico, los búferes transitorios y la imprecisión natural de
# una lectura tomada antes de arrancar todos los hijos.
_WORKER_MEMORY_MB = 1024
# Memoria que se deja libre para el sistema, la GUI y las salidas PDF. Escala
# con el equipo entre un suelo y un techo: en uno de 16 GB el suelo de 1,5 GB
# deja el margen demasiado corto en cuanto el usuario abre el visor de CSV o un
# navegador, y quedarse sin memoria a mitad de un batch cuesta mucho más que un
# proceso menos. El techo evita el error contrario: en un equipo de 32 GB una
# fracción fija reservaría casi 5 GB y renunciaría a procesos sin motivo, ya
# que lo que hay que cubrir (sistema, interfaz, salidas) no crece con la RAM.
_RESERVED_MEMORY_MB = 1536
_RESERVED_MEMORY_RATIO = 0.15
_MAX_RESERVED_MEMORY_MB = 3072


@dataclass(frozen=True)
class CoreTopology:
    """Cómo son los núcleos de esta máquina, no de la del desarrollo.

    ``performance`` y ``efficiency`` cuentan núcleos *físicos* de cada clase.
    En un procesador homogéneo todos caen en ``performance`` y ``efficiency``
    queda en cero.
    """

    logical: int
    physical: int
    performance: int
    efficiency: int

    @property
    def hybrid(self) -> bool:
        return self.efficiency > 0

    def describe(self) -> str:
        if not self.hybrid:
            return (
                f"{self.physical} núcleos / {self.logical} hilos (homogéneo)"
            )
        return (
            f"{self.performance} núcleos de rendimiento + "
            f"{self.efficiency} de eficiencia / {self.logical} hilos"
        )


def available_cpu_threads() -> int:
    """Devuelve el número de hilos lógicos disponibles para la aplicación."""
    return max(1, os.cpu_count() or 1)


def total_memory_mb() -> int:
    """Memoria física total del equipo en MB (0 si no se puede determinar)."""
    status = _memory_status()
    return int(status.ullTotalPhys // (1024 * 1024)) if status else 0


def core_topology() -> CoreTopology:
    """Núcleos físicos por clase de rendimiento e hilos lógicos.

    Los equipos donde corre esto no son el equipo donde se midió: un i7 de
    12ª generación reparte sus 20 hilos entre núcleos de rendimiento (con SMT,
    dos hilos cada uno) y núcleos de eficiencia (uno), que no rinden igual.
    Windows lo expone en ``EfficiencyClass``; la clase más alta es la de
    rendimiento. Si la API no está disponible se supone homogéneo, que es lo
    que el cálculo asumía antes.
    """
    logical = available_cpu_threads()
    try:
        cores = _enumerate_cores()
    except (AttributeError, OSError, ValueError):
        cores = []
    if not cores:
        return CoreTopology(logical, logical, logical, 0)
    best = max(efficiency for efficiency, _threads in cores)
    performance = sum(1 for efficiency, _t in cores if efficiency == best)
    counted = sum(threads for _e, threads in cores)
    return CoreTopology(
        logical=counted or logical,
        physical=len(cores),
        performance=performance,
        efficiency=len(cores) - performance,
    )


def _enumerate_cores() -> list[tuple[int, int]]:
    """(clase de eficiencia, hilos) de cada núcleo físico, vía Win32."""
    relation_processor_core = 0
    kernel32 = ctypes.windll.kernel32
    length = wintypes.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(
        relation_processor_core, None, ctypes.byref(length)
    )
    if not length.value:
        return []
    buffer = (ctypes.c_byte * length.value)()
    if not kernel32.GetLogicalProcessorInformationEx(
        relation_processor_core, buffer, ctypes.byref(length)
    ):
        return []

    cores: list[tuple[int, int]] = []
    address = ctypes.addressof(buffer)
    offset = 0
    while offset < length.value:
        relationship = ctypes.c_uint32.from_address(address + offset).value
        size = ctypes.c_uint32.from_address(address + offset + 4).value
        if size <= 0:
            break
        if relationship == relation_processor_core:
            # PROCESSOR_RELATIONSHIP: Flags(1) EfficiencyClass(1)
            # Reserved[20] GroupCount(2) GroupMask[]; GROUP_AFFINITY mide 16.
            base = address + offset + 8
            efficiency = ctypes.c_ubyte.from_address(base + 1).value
            groups = ctypes.c_uint16.from_address(base + 22).value
            threads = 0
            for group in range(max(1, groups)):
                mask = ctypes.c_size_t.from_address(
                    base + 24 + group * 16
                ).value
                threads += bin(mask).count("1")
            cores.append((efficiency, threads))
        offset += size
    return cores


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_status() -> "_MemoryStatus | None":
    """Lee el estado de memoria de Windows, o None fuera de Windows."""
    try:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
            ctypes.byref(status)
        ):
            return None
        return status
    except (AttributeError, OSError):
        # Cualquier plataforma sin esta API (pruebas fuera de Windows).
        return None


def available_memory_mb() -> int:
    """Memoria física libre en MB (0 si no se puede determinar).

    Usa solo la API de Windows a través de ``ctypes`` para no añadir
    dependencias al paquete portable.
    """
    status = _memory_status()
    return int(status.ullAvailPhys // (1024 * 1024)) if status else 0


def reserved_memory_mb() -> int:
    """Memoria que no se reparte: sistema, interfaz y salidas.

    Proporcional al equipo, con un suelo. En uno de 16 GB el suelo fijo de
    1,5 GB dejaba el margen demasiado corto (basta con que el usuario abra el
    visor de CSV o un navegador) y quedarse sin memoria a mitad de un batch
    cuesta mucho más que renunciar a un proceso.
    """
    total = total_memory_mb()
    if total <= 0:
        return _RESERVED_MEMORY_MB
    scaled = int(total * _RESERVED_MEMORY_RATIO)
    return min(max(_RESERVED_MEMORY_MB, scaled), _MAX_RESERVED_MEMORY_MB)


def _memory_worker_cap() -> int:
    """Cuántos procesos OCR caben en la memoria libre del equipo."""
    available = available_memory_mb()
    if available <= 0:
        return _MAX_WORKERS
    usable = available - reserved_memory_mb()
    if usable <= 0:
        return 1
    return max(1, usable // _WORKER_MEMORY_MB)


def recommended_parallelism(total_threads: int | None = None) -> tuple[int, int]:
    """Distribuye un presupuesto de hilos entre procesos e hilos internos.

    ``total_threads`` es el presupuesto de hilos lógicos que el usuario cede
    al procesamiento, **no** el tamaño del equipo. Son cantidades distintas:
    la GUI reserva un hilo para la interfaz y además deja elegir menos. El
    tamaño del equipo se consulta siempre a la máquina.

    Confundirlas era un error caro. Al estimar los núcleos como la mitad del
    presupuesto, pedir 3 hilos daba ``(1 worker x 3 hilos)`` (como si el
    equipo tuviera un solo núcleo) cuando lo correcto son 3 procesos: medido,
    0.116 frente a 0.239 páginas/s, la mitad de velocidad. Con 2 hilos el
    mismo fallo costaba 1.58x.

    El objetivo es **un proceso por hilo lógico del presupuesto**, que es la
    configuración más rápida medida en los tres tamaños de equipo probados.
    Los límites reales son la memoria libre y el tope duro, no los núcleos
    físicos: pararse en ellos costaba un 17% en el equipo de 6 núcleos y un
    34% en el de 4.

    Los hilos internos solo se reparten con lo que sobra cuando la memoria
    impide crear más procesos (ese proceso aprovecha los hilos restantes en
    vez de desperdiciarlos) y nunca pasan de ``_MAX_ENGINE_THREADS``, porque
    la inferencia satura en 3.
    """
    total = max(
        1,
        int(total_threads)
        if total_threads is not None
        else available_cpu_threads(),
    )
    workers = min(
        _MAX_WORKERS,
        total,
        available_cpu_threads(),
        _memory_worker_cap(),
    )
    threads = min(max(1, total // workers), _MAX_ENGINE_THREADS)
    return workers, threads
