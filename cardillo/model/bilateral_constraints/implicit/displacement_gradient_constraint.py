import numpy as np


class Displacement_constraint:
    def __init__(self, subsystem, la_mesh, srf_id=None, edge_id=None, x=0, disp=0):
        self.subsystem = subsystem
        self.srf_id = srf_id
        self.edge_id = edge_id
        if srf_id:
            self.con_mesh = subsystem.mesh.surface_mesh[srf_id]
        elif edge_id:
            self.con_mesh = subsystem.mesh.edge_mesh[edge_id]
        self.la_mesh = la_mesh
        self.nla_g = la_mesh.nn
        self.la_g0 = np.zeros(self.nla_g)
        self.x = x
        if not callable(disp):
            self.dq = lambda t, q: disp
        else:
            self.dq = disp

    def assembler_callback(self):
        self.qDOF = self.subsystem.qDOF
        if self.srf_id:
            self.con_zDOF = self.subsystem.mesh.surface_qDOF[self.srf_id].ravel()
        elif self.edge_id:
            self.con_zDOF = self.subsystem.mesh.edge_qDOF[self.edge_id].ravel()
        self.Z_con = self.subsystem.Z[self.con_zDOF]
        self.nz_con = len(self.Z_con)
        self.con_z_elDOF_global = self.con_zDOF[self.con_mesh.elDOF]
        self.w_J0 = self.con_mesh.reference_mappings(self.Z_con)

        # TODO: Required for dynamics and sorting of generalized force directions.
        # self.uDOF = self.subsystem.uDOF

    def g_el(self, t, qe, Qe, el):
        displacement = self.dq(t, qe)

        ge = np.zeros(self.la_mesh.nn_el)
        for i in range(self.con_mesh.nqp):
            w_J0 = self.w_J0[el, i]
            N_la_eli = self.la_mesh.N[el, i]
            N_eli = self.con_mesh.N[el, i]
            dqi = 0
            for a in range(self.con_mesh.nn_el):
                dqi += N_eli[a] * (
                    qe[self.x * self.con_mesh.nn_el + a]
                    - Qe[self.x * self.con_mesh.nn_el + a]
                    - displacement
                )
            for a_tilde in range(self.la_mesh.nn_el):
                ge[a_tilde] += N_la_eli[a_tilde] * dqi * w_J0
        return ge

    def g(self, t, q):
        g = np.zeros(self.nla_g)
        z = self.subsystem.z(t, q)
        Z = self.subsystem.Z
        for el in range(self.con_mesh.nel):
            elDOF_el = self.con_z_elDOF_global[el]
            qe = z[elDOF_el]
            Qe = Z[elDOF_el]
            la_elDOF_el = self.la_mesh.elDOF[el]
            g[la_elDOF_el] += self.g_el(t, qe, Qe, el)
        return g

    def g_el_q(self, t, qe, el):
        ge_q = np.zeros((self.la_mesh.nn_el, qe.shape[0]))
        for i in range(self.con_mesh.nqp):
            N_la_eli = self.la_mesh.N[el, i]
            N_eli = self.con_mesh.N[el, i]
            w_J0 = self.w_J0[el, i]
            for a in range(self.con_mesh.nn_el):
                for a_tilde in range(self.la_mesh.nn_el):
                    ge_q[a_tilde, self.con_mesh.nodalDOF[a, self.x]] += (
                        N_la_eli[a_tilde] * N_eli[a] * w_J0
                    )
        return ge_q

    def g_q_dense(self, t, q):
        g_z = np.zeros((self.nla_g, self.subsystem.nz))
        z = self.subsystem.z(t, q)
        for el in range(self.con_mesh.nel):
            elDOF_el = self.con_z_elDOF_global[el]
            qe = z[elDOF_el]
            la_elDOF_el = self.la_mesh.elDOF[el]
            g_z[np.ix_(la_elDOF_el, elDOF_el)] += self.g_el_q(t, qe, el)
        return g_z[:, self.subsystem.fDOF]  # g_q = g_z[:, self.subsystem.fDOF]

    def g_q(self, t, q, coo):
        coo.extend(self.g_q_dense(t, q), (self.la_gDOF, self.qDOF))

    def W_g(self, t, q, coo):
        # TODO: uDOF instead of qDOF!
        coo.extend(self.g_q_dense(t, q).T, (self.qDOF, self.la_gDOF))

    # def Wla_g_q_el(self, t, qel, lael, el):
    #     Wla_g_q_el = np.zeros((qel.shape[0], qel.shape[0]))
    #     for i in range(self.con_mesh.nqp):
    #         # N_la_eli = self.la_mesh.N[el, i]
    #         # w_J0 = self.w_J0[el, i]
    #         idx = np.array([self.x + 3 * i for i in range(int(qel.shape[0] / 3))])
    #         for a in range(self.con_mesh.nn_el):
    #             for a_tilde in range(self.la_mesh.nn_el):
    #                 # la_a_tilde = lael[a_tilde]
    #                 Wla_g_q_el[self.con_mesh.nodalDOF[a, self.x], idx] += 0
    #     return Wla_g_q_el

    def Wla_g_q(self, t, q, la_g, coo):
        pass
        # Wla_g_q = np.zeros((self.subsystem.nz, self.subsystem.nz))
        # z = self.subsystem.z(t, q)
        # for el in range(self.con_mesh.nel):
        #     qel = z[self.con_z_elDOF_global[el]]
        #     la_elDOF = self.la_mesh.elDOF[el]
        #     lael = la_g[la_elDOF]
        #     Wla_g_q[
        #         np.ix_(self.con_z_elDOF_global[el], self.con_z_elDOF_global[el])
        #     ] += self.Wla_g_q_el(t, qel, lael, el)

        # # TODO: Replace first qDOF with uDOF!
        # coo.extend(
        #     Wla_g_q[np.ix_(self.subsystem.fDOF, self.subsystem.fDOF)],
        #     (self.qDOF, self.qDOF),
        # )


# TODO: Only constraint on gradient and not position (possible on dirichlet boundary?)
class Gradient_constraint:
    def __init__(self, subsystem, la_mesh, con_id=0, x=0, _X=0):
        self.subsystem = subsystem
        self.mesh = self.subsystem.mesh
        self.con_mesh = subsystem.mesh.surface_mesh[con_id]
        self.la_mesh = la_mesh
        self.nla_g = la_mesh.nn
        self.la_g0 = np.zeros(self.nla_g)
        self.x = x
        self.con_id = con_id
        self.bc_el = self.mesh.bc_el[con_id]
        self.Nb = self.mesh.Nb[con_id]
        self.Nb_X = self.mesh.Nb_X(self.subsystem.Z, con_id)
        # self.Nb_xixi = self.mesh.Nb_xixi[con_id]
        self._X = _X

    def assembler_callback(self):
        self.qDOF = self.subsystem.qDOF
        self.con_qDOF = self.subsystem.mesh.surface_qDOF[self.con_id].ravel()
        self.fDOF = self.subsystem.fDOF
        self.con_fDOF = np.intersect1d(self.con_qDOF, self.fDOF)
        self.Q_con = self.subsystem.Z[self.con_qDOF]
        self.nz_con = len(self.Q_con)
        self.w_J0 = self.con_mesh.reference_mappings(self.Q_con)
        self.con_elDOF_global = self.con_qDOF[self.con_mesh.elDOF]
        # self.uDOF = self.subsystem.uDOF

    def g_el(self, t, qe, Qe, el):
        ge = np.zeros(self.la_mesh.nn_el)
        for i in range(self.con_mesh.nqp):
            w_J0 = self.w_J0[el, i]
            N_la_eli = self.la_mesh.N[el, i]
            Nb_X_eli = self.Nb_X[el, i]
            dqi_X = 0
            # gradient difference in direction X
            for a in range(self.mesh.nn_el):
                dqi_X += Nb_X_eli[a, self._X] * (
                    +qe[self.x * self.mesh.nn_el + a] - Qe[self.x * self.mesh.nn_el + a]
                )
            for a_tilde in range(self.la_mesh.nn_el):
                ge[a_tilde] += N_la_eli[a_tilde] * dqi_X * w_J0
        return ge

    def g(self, t, q):
        g = np.zeros(self.nla_g)
        z = self.subsystem.z(t, q)
        Z = self.subsystem.Z
        for el in range(self.con_mesh.nel):
            elDOF_el = self.mesh.elDOF[self.bc_el[el]]
            qe = z[elDOF_el]
            Qe = Z[elDOF_el]
            la_elDOF_el = self.la_mesh.elDOF[el]
            g[la_elDOF_el] += self.g_el(t, qe, Qe, el)
        return g

    def g_el_q(self, t, qe, el):
        ge_q = np.zeros((self.la_mesh.nn_el, qe.shape[0]))
        for i in range(self.con_mesh.nqp):
            N_la_eli = self.la_mesh.N[el, i]
            Nb_X_eli = self.Nb_X[el, i]
            w_J0 = self.w_J0[el, i]
            for a in range(self.mesh.nn_el):
                for a_tilde in range(self.con_mesh.nn_el):
                    # ge_q[np.ix_(np.array([a_tilde]), self.mesh.nodalDOF[a])] += N_la_eli[a_tilde] * detF * np.einsum('kl,l', F_inv.T,  N_X_eli[a]) * w_J0
                    ge_q[a_tilde, self.mesh.nodalDOF[a, self.x]] += (
                        N_la_eli[a_tilde] * Nb_X_eli[a, self._X] * w_J0
                    )
        return ge_q

    def g_q_dense(self, t, q):
        g_q = np.zeros((self.nla_g, self.subsystem.nz))
        z = self.subsystem.z(t, q)
        for el in range(self.con_mesh.nel):
            elDOF_el = self.mesh.elDOF[self.bc_el[el]]
            qe = z[elDOF_el]
            la_elDOF_el = self.la_mesh.elDOF[el]
            g_q[np.ix_(la_elDOF_el, elDOF_el)] += self.g_el_q(t, qe, el)
        return g_q[:, self.subsystem.fDOF]

    def g_q(self, t, q, coo):
        coo.extend(self.g_q_dense(t, q), (self.la_gDOF, self.qDOF))

    def W_g(self, t, q, coo):
        coo.extend(self.g_q_dense(t, q).T, (self.qDOF, self.la_gDOF))

    def Wla_g_q_el(self, t, qel, lael, el):
        Wla_g_q_el = np.zeros((qel.shape[0], qel.shape[0]))
        for i in range(self.con_mesh.nqp):
            idx = np.array([self.x + 3 * i for i in range(int(qel.shape[0] / 3))])
            for a in range(self.con_mesh.nn_el):
                for a_tilde in range(self.la_mesh.nn_el):
                    Wla_g_q_el[self.con_mesh.nodalDOF[a, self.x], idx] += 0
        return Wla_g_q_el

    def Wla_g_q(self, t, q, la_g, coo):
        Wla_g_q = np.zeros((self.subsystem.nz, self.subsystem.nz))
        z = self.subsystem.z(t, q)
        for el in range(self.con_mesh.nel):
            elDOF_el = self.mesh.elDOF[self.bc_el[el]]
            qel = z[elDOF_el]
            la_elDOF = self.la_mesh.elDOF[el]
            lael = la_g[la_elDOF]
            Wla_g_q[np.ix_(elDOF_el, elDOF_el)] += self.Wla_g_q_el(t, qel, lael, el)

        coo.extend(
            Wla_g_q[np.ix_(self.subsystem.fDOF, self.subsystem.fDOF)],
            (self.qDOF, self.qDOF),
        )
