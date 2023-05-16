# TODO: only works with 3D continua
class DistributedForce1DContinuum:
    def __init__(self, force_distr1D, subsystem, edge_idx):
        if not callable(force_distr1D):
            self.force_distr1D = lambda t, xi: force_distr1D
        else:
            self.force_distr1D = force_distr1D
        self.subsystem = subsystem
        self.edge_idx = edge_idx
        self.srf_idx = self.edge_idx // 4
        self.srf_edge_idx = self.edge_idx % 4
        self.srf_mesh = self.subsystem.mesh.surface_mesh[self.srf_idx]
        self.edge_mesh = self.srf_mesh.edge_mesh[self.srf_edge_idx]

    def assembler_callback(self):
        self.qDOF = self.subsystem.qDOF
        self.uDOF = self.subsystem.uDOF
        self.srf_qDOF = self.subsystem.mesh.surface_qDOF[self.srf_idx].ravel()
        self.edge_qDOF = self.srf_qDOF[
            self.srf_mesh.edge_qDOF[self.srf_edge_idx].ravel()
        ]
        self.Q_edge = self.subsystem.Z[self.edge_qDOF]
        self.edge_w_J0 = self.edge_mesh.reference_mappings(self.Q_edge)

    def E_pot(self, t, q):
        return self.subsystem.force_distr1D_pot(
            t, q, self.force_distr1D, self.edge_idx, self.edge_w_J0
        )

    def h(self, t, q, u):
        return self.subsystem.force_distr1D(
            t, q, self.force_distr1D, self.edge_idx, self.edge_qDOF, self.edge_w_J0
        )

    def h_q(self, t, q):
        return self.subsystem.force_distr1D_q(
            t, q, self.force_distr1D, self.edge_idx, self.edge_w_J0
        )
