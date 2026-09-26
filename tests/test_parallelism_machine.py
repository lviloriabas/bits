"""El reparto se decide en la maquina que ejecuta, no en la del desarrollo.

Los equipos de destino no se parecen al de las mediciones: 20 hilos
repartidos entre nucleos de rendimiento y de eficiencia, y 16 GB de RAM en
vez de 32. Estas pruebas fijan que el calculo dependa de lo que se mide en
tiempo de ejecucion -topologia y memoria- y no de constantes heredadas.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.core import parallelism
from app.core.parallelism import (
    CoreTopology,
    core_topology,
    recommended_parallelism,
    reserved_memory_mb,
)


def _machine(threads: int, total_mb: int, free_mb: int):
    """Simula un equipo completo: hilos, RAM total y RAM libre."""
    return (
        mock.patch.object(parallelism, "available_cpu_threads",
                          return_value=threads),
        mock.patch.object(parallelism, "total_memory_mb",
                          return_value=total_mb),
        mock.patch.object(parallelism, "available_memory_mb",
                          return_value=free_mb),
    )


class TestTopology(unittest.TestCase):
    def test_real_machine_is_coherent(self):
        topology = core_topology()
        self.assertGreaterEqual(topology.logical, 1)
        self.assertGreaterEqual(topology.physical, 1)
        self.assertLessEqual(topology.physical, topology.logical)
        self.assertEqual(
            topology.performance + topology.efficiency, topology.physical
        )
        self.assertIn("hilos", topology.describe())

    def test_homogeneous_machine_reports_no_efficiency_cores(self):
        topology = CoreTopology(logical=12, physical=6, performance=6,
                                efficiency=0)
        self.assertFalse(topology.hybrid)
        self.assertIn("homogéneo", topology.describe())

    def test_hybrid_machine_is_described_by_class(self):
        # i7 de 12a generacion: 8 nucleos P con SMT + 4 nucleos E.
        topology = CoreTopology(logical=20, physical=12, performance=8,
                                efficiency=4)
        self.assertTrue(topology.hybrid)
        self.assertIn("8 núcleos de rendimiento", topology.describe())
        self.assertIn("4 de eficiencia", topology.describe())

    def test_without_the_api_it_assumes_homogeneous(self):
        with mock.patch.object(parallelism, "_enumerate_cores",
                               side_effect=OSError), \
                mock.patch.object(parallelism, "available_cpu_threads",
                                  return_value=20):
            topology = core_topology()
        self.assertEqual(topology.logical, 20)
        self.assertFalse(topology.hybrid)


class TestReservedMemoryScales(unittest.TestCase):
    def test_small_machine_keeps_the_floor(self):
        with mock.patch.object(parallelism, "total_memory_mb",
                               return_value=8192):
            self.assertEqual(reserved_memory_mb(),
                             parallelism._RESERVED_MEMORY_MB)

    def test_sixteen_gigabytes_reserves_more_than_the_floor(self):
        with mock.patch.object(parallelism, "total_memory_mb",
                               return_value=16384):
            reserved = reserved_memory_mb()
        self.assertGreater(reserved, parallelism._RESERVED_MEMORY_MB)
        self.assertLessEqual(reserved, parallelism._MAX_RESERVED_MEMORY_MB)

    def test_a_large_machine_does_not_reserve_without_limit(self):
        """Lo que se cubre (sistema, interfaz, salidas) no crece con la RAM."""
        with mock.patch.object(parallelism, "total_memory_mb",
                               return_value=131072):
            self.assertEqual(reserved_memory_mb(),
                             parallelism._MAX_RESERVED_MEMORY_MB)

    def test_without_a_reading_it_falls_back_to_the_floor(self):
        with mock.patch.object(parallelism, "total_memory_mb", return_value=0):
            self.assertEqual(reserved_memory_mb(),
                             parallelism._RESERVED_MEMORY_MB)


class TestAdaptsToTheMachine(unittest.TestCase):
    def test_twenty_threads_are_not_capped_at_sixteen(self):
        """El tope duro no debe decidir en un equipo de 20 hilos."""
        patches = _machine(threads=20, total_mb=16384, free_mb=99999)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        workers, threads = recommended_parallelism()
        self.assertEqual((workers, threads), (20, 1))

    def test_memory_is_what_limits_the_target_machine(self):
        patches = _machine(threads=20, total_mb=16384, free_mb=10000)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        workers, _threads = recommended_parallelism()
        self.assertLess(workers, 20)
        budget = workers * parallelism._WORKER_MEMORY_MB
        self.assertLessEqual(budget, 10000 - reserved_memory_mb())

    def test_no_agota_el_margen_con_trece_gigabytes_libres(self):
        """El caso real con 11 procesos paginaba modelos y se volvió lento."""
        patches = _machine(threads=12, total_mb=32691, free_mb=12999)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        workers, threads = recommended_parallelism(11)
        self.assertEqual((workers, threads), (9, 1))
        self.assertGreaterEqual(
            12999 - reserved_memory_mb()
            - workers * parallelism._WORKER_MEMORY_MB,
            500,
        )

    def test_the_same_machine_with_less_free_memory_uses_fewer_processes(self):
        counts = []
        for free in (12000, 10000, 8000, 6000):
            patches = _machine(threads=20, total_mb=16384, free_mb=free)
            for patch in patches:
                patch.start()
            counts.append(recommended_parallelism()[0])
            for patch in patches:
                patch.stop()
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_a_leftover_budget_goes_to_engine_threads(self):
        """Si la memoria corta los procesos, los hilos sobrantes se usan."""
        patches = _machine(threads=20, total_mb=16384, free_mb=8000)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        workers, threads = recommended_parallelism()
        self.assertGreater(threads, 1)
        self.assertLessEqual(workers * threads, 20)


if __name__ == "__main__":
    unittest.main()
