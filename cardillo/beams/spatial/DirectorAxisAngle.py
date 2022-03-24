import numpy as np
import meshio
import os

from cardillo.utility.coo import Coo
from cardillo.discretization.B_spline import KnotVector
from cardillo.discretization.lagrange import Node_vector
from cardillo.discretization.mesh1D import Mesh1D
from cardillo.math import norm, cross3, skew2ax, skew2ax_A, approx_fprime
from PyRod.math.rotations import (
    rodriguez,
    rodriguez_inv,
    tangent_map,
    inverse_tangent_map,
    tangent_map_s,
)


class DirectorAxisAngle:
    def __init__(
        self,
        material_model,
        A_rho0,
        B_rho0,
        C_rho0,
        polynomial_degree_r,
        polynomial_degree_psi,
        nquadrature,
        nelement,
        Q,
        q0=None,
        u0=None,
        basis="B-spline",
    ):

        # beam properties
        self.materialModel = material_model  # material model
        # TODO: Adapt inertia properties.
        self.A_rho0 = A_rho0  # line density
        self.B_rho0 = B_rho0  # first moment
        self.C_rho0 = C_rho0  # second moment

        # material model
        self.material_model = material_model

        # discretization parameters
        self.polynomial_degree_r = polynomial_degree_r  # polynomial degree centerline
        self.polynomial_degree_psi = (
            polynomial_degree_psi  # polynomial degree axis angle
        )
        self.nquadrature = nquadrature  # number of quadrature points
        self.nelement = nelement  # number of elements

        self.basis = basis
        if basis == "B-spline":
            self.knot_vector_r = KnotVector(polynomial_degree_r, nelement)
            self.knot_vector_psi = KnotVector(polynomial_degree_psi, nelement)
            self.nnode_r = nnode_r = nelement + polynomial_degree_r  # number of nodes
            self.nnode_psi = nnode_psi = (
                nelement + polynomial_degree_psi
            )  # number of nodes
        elif basis == "lagrange":
            self.knot_vector_r = Node_vector(polynomial_degree_r, nelement)
            self.knot_vector_psi = Node_vector(polynomial_degree_psi, nelement)
            self.nnode_r = nnode_r = (
                nelement * polynomial_degree_r + 1
            )  # number of nodes
            self.nnode_psi = nnode_psi = (
                nelement * polynomial_degree_psi + 1
            )  # number of nodes
        else:
            raise RuntimeError(f'wrong basis: "{basis}" was chosen')

        # number of nodes per element
        self.nnodes_element_r = nnodes_element_r = polynomial_degree_r + 1
        self.nnodes_element_psi = nnodes_element_psi = polynomial_degree_psi + 1

        # number of degrees of freedom per node
        self.nq_node_r = nq_node_r = 3
        self.nq_node_psi = nq_node_psi = 3

        self.mesh_r = Mesh1D(
            self.knot_vector_r,
            nquadrature,
            derivative_order=1,
            basis=basis,
            nq_node=nq_node_r,
        )
        self.mesh_psi = Mesh1D(
            self.knot_vector_psi,
            nquadrature,
            derivative_order=1,
            basis=basis,
            nq_node=nq_node_psi,
        )

        self.nq_r = nq_r = nnode_r * nq_node_r
        self.nq_psi = nq_psi = nnode_psi * nq_node_psi
        self.nq = nq_r + nq_psi  # total number of generalized coordinates
        self.nu = self.nq
        self.nq_elelement = (
            nnodes_element_r * nq_node_r + nnodes_element_psi * nq_node_psi
        )  # total number of generalized coordinates per element
        self.nq_el_r = nnodes_element_r * nq_node_r
        self.nq_el_psi = nnodes_element_psi * nq_node_psi

        # connectivity matrices for both meshes
        self.elDOF_r = self.mesh_r.elDOF
        self.elDOF_psi = self.mesh_psi.elDOF + nq_r
        self.nodalDOF_r = (
            np.arange(self.nq_node_r * self.nnode_r)
            .reshape(self.nq_node_r, self.nnode_r)
            .T
        )
        self.nodalDOF_psi = (
            np.arange(self.nq_node_psi * self.nnode_psi)
            .reshape(self.nq_node_psi, self.nnode_psi)
            .T
            + nq_r
        )

        # build global elDOF connectivity matrix
        self.elDOF = np.zeros((nelement, self.nq_elelement), dtype=int)
        for el in range(nelement):
            self.elDOF[el, : self.nq_el_r] = self.elDOF_r[el]
            self.elDOF[el, self.nq_el_r :] = self.elDOF_psi[el]

        # degrees of freedom on element level
        self.rDOF = np.arange(0, nq_node_r * nnodes_element_r)
        self.psiDOF = (
            np.arange(0, nq_node_psi * nnodes_element_psi)
            + nq_node_r * nnodes_element_r
        )

        # shape functions and their first derivatives
        self.N_r = self.mesh_r.N
        self.N_r_xi = self.mesh_r.N_xi
        self.N_psi = self.mesh_psi.N
        self.N_psi_xi = self.mesh_psi.N_xi

        # quadrature points
        self.qp = self.mesh_r.qp  # quadrature points
        self.qw = self.mesh_r.wp  # quadrature weights

        # reference generalized coordinates, initial coordinates and initial velocities
        self.Q = Q
        self.q0 = Q.copy() if q0 is None else q0
        self.u0 = np.zeros(self.nu) if u0 is None else u0

        # evaluate shape functions at specific xi
        self.basis_functions_r = self.mesh_r.eval_basis
        self.basis_functions_psi = self.mesh_psi.eval_basis

        # reference generalized coordinates, initial coordinates and initial velocities
        self.Q = Q  # reference configuration
        self.q0 = Q.copy() if q0 is None else q0  # initial configuration
        self.u0 = np.zeros(self.nu) if u0 is None else u0  # initial velocities

        # precompute values of the reference configuration in order to save computation time
        # J in Harsch2020b (5)
        self.J = np.zeros((nelement, nquadrature))
        # dilatation and shear strains of the reference configuration
        self.Gamma0 = np.zeros((nelement, nquadrature, 3))
        # curvature of the reference configuration
        self.Kappa0 = np.zeros((nelement, nquadrature, 3))

        # fix evaluation points of rotation vectors for each element since we
        # want to evaluate their derivatives but do not have them on fixed
        # conrtol nodes
        knotvector_psi_data = self.knot_vector_psi.element_data
        self.xi_el_psi = lambda el: np.linspace(
            knotvector_psi_data[el],
            knotvector_psi_data[el + 1],
            num=self.nnodes_element_psi,
        )

        for el in range(nelement):
            # precompute quantities of the reference configuration
            qe = self.Q[self.elDOF[el]]
            qe_r = qe[self.rDOF]
            qe_psi = qe[self.psiDOF]

            # since we cannot extract the derivatives of nodal rotation vectors
            # we compute their values on fixed xis's
            # psi = np.array([])
            # for no in range(self.nnode_psi):
            psis = np.zeros((self.nnode_psi, 3))
            # psi_xis = np.zeros((self.nnode_psi, 3))
            Rs = np.zeros((self.nnode_psi, 3, 3))
            # R_xis = np.zeros((self.nnode_psi, 3, 3))
            for i, xii in enumerate(self.xi_el_psi(el)):
                # TODO: Precompute these shape functions.
                NN_psi_i = self.stack3psi(self.basis_functions_psi(xii)[0])
                # NN_psi_xii = self.stack3psi(self.basis_functions_psi(xii)[1])
                psi = NN_psi_i @ qe_psi
                # psi_xi = NN_psi_xii @ qe_psi
                psis[i] = psi
                # psi_xis[i] = psi_xi
                # TODO: Do we require psi_s here? This requires the evaluation
                #       of J(xi) at the fixed xi values.

                # evaluate rotation and extract directors
                R = rodriguez(psi)
                d1, d2, d3 = R.T
                Rs[i] = R
                # # TODO: This is not Kapp_i since 1 / J is missing!
                # Kappa_i = tangent_map(psi) @ psi_xi
                # d1_xi = cross3(Kappa_i, d1)
                # d2_xi = cross3(Kappa_i, d2)
                # d3_xi = cross3(Kappa_i, d3)
                # R_xi = np.vstack((d1_xi, d2_xi, d3_xi)).T
                # R_xis[i] = R_xi

                # # TODO: Remove debugging prints.
                # print(f"psi: {psi}")
                # print(f"psi_xi: {psi_xi}")
                # print(f"Kappa_i * J: {Kappa_i}")
                # print(f"R:\n{R}")
                # print(f"R_xi:\n{R_xi}")
                # print(f"")

            for i in range(nquadrature):
                # build matrix of shape function derivatives
                NN_r_xii = self.stack3r(self.N_r_xi[el, i])
                NN_psi_i = self.stack3psi(self.N_psi[el, i])
                NN_psi_xii = self.stack3psi(self.N_psi_xi[el, i])

                # Interpolate necessary quantities. The derivatives are evaluated w.r.t.
                # the parameter space \xi and thus need to be transformed later
                r_xi = NN_r_xii @ qe_r
                Ji = ji = norm(r_xi)
                self.J[el, i] = Ji

                # build qe's for each director and their derivatives?
                qe_d1 = Rs[:, :, 0].reshape(-1, order="F")  # TODO: Ordering?
                qe_d2 = Rs[:, :, 1].reshape(-1, order="F")  # TODO: Ordering?
                qe_d3 = Rs[:, :, 2].reshape(-1, order="F")  # TODO: Ordering?

                # interpolate these directors using the existing shae functions
                # of the rotation vector
                d1 = NN_psi_i @ qe_d1
                d2 = NN_psi_i @ qe_d2
                d3 = NN_psi_i @ qe_d3
                d1_xi = NN_psi_xii @ qe_d1
                d2_xi = NN_psi_xii @ qe_d2
                d3_xi = NN_psi_xii @ qe_d3

                # compute derivatives w.r.t. the arc lenght parameter s
                r_s = r_xi / Ji
                d1_s = d1_xi / Ji
                d2_s = d2_xi / Ji
                d3_s = d3_xi / Ji

                # axial and shear strains
                self.Gamma0[el, i] = np.array([r_s @ d1, r_s @ d2, r_s @ d3])

                # torsional and flexural strains
                self.Kappa0[el, i] = np.array(
                    [
                        0.5 * (d3 @ d2_s - d2 @ d3_s),
                        0.5 * (d1 @ d3_s - d3 @ d1_s),
                        0.5 * (d2 @ d1_s - d1 @ d2_s),
                    ]
                )

    @staticmethod
    def straight_configuration(
        polynomial_degree_r,
        polynomial_degree_psi,
        nEl,
        L,
        greville_abscissae=True,
        r_OP=np.zeros(3),
        A_IK=np.eye(3),
        basis="B-spline",
    ):
        if basis == "B-spline":
            nn_r = polynomial_degree_r + nEl
            nn_psi = polynomial_degree_psi + nEl
        elif basis == "lagrange":
            nn_r = polynomial_degree_r * nEl + 1
            nn_psi = polynomial_degree_psi * nEl + 1
        else:
            raise RuntimeError(f'wrong basis: "{basis}" was chosen')

        x0 = np.linspace(0, L, num=nn_r)
        y0 = np.zeros(nn_r)
        z0 = np.zeros(nn_r)
        if greville_abscissae and basis == "B-spline":
            kv = KnotVector.uniform(polynomial_degree_r, nEl)
            for i in range(nn_r):
                x0[i] = np.sum(kv[i + 1 : i + polynomial_degree_r + 1])
            x0 = x0 * L / polynomial_degree_r

        r0 = np.vstack((x0, y0, z0)).T
        for i, r0i in enumerate(r0):
            x0[i], y0[i], z0[i] = r_OP + A_IK @ r0i

        # we have to extract the rotation vector from the given rotation matrix
        psi = rodriguez_inv(A_IK)
        psi0 = np.repeat(psi, nn_psi)
        # TODO: This is wrong. How do we get the Greville abszissae for the
        #       rotation vector?
        #       Since we have a straight beem all rotation vectors are the
        #       same. Thus, Greville abszissae should not be necessary!
        # if greville_abscissae and basis == "B-spline":
        #     kv = KnotVector.uniform(polynomial_degree_psi, nEl)
        #     for i in range(nn_r):
        #         psi0[i] = np.sum(kv[i + 1 : i + polynomial_degree_r + 1])
        #     psi0 = psi0 * L / polynomial_degree_psi

        return np.concatenate([x0, y0, z0, psi0])

    def element_number(self, xi):
        # note the elements coincide for both meshes!
        return self.knot_vector_r.element_number(xi)[0]

    def stack3r(self, N):
        nn_el = self.nnodes_element_r
        NN = np.zeros((3, self.nq_el_r))
        NN[0, :nn_el] = N
        NN[1, nn_el : 2 * nn_el] = N
        NN[2, 2 * nn_el :] = N
        return NN

    def stack3psi(self, N):
        nn_el = self.nnodes_element_psi
        NN = np.zeros((3, self.nq_el_psi))
        NN[0, :nn_el] = N
        NN[1, nn_el : 2 * nn_el] = N
        NN[2, 2 * nn_el :] = N
        return NN

    def assembler_callback(self):
        self.__M_coo()

    #########################################
    # equations of motion
    #########################################
    # TODO:
    def M_el(self, el):
        Me = np.zeros((self.nq_elelement, self.nq_elelement))

        # for i in range(self.nquadrature):
        #     # build matrix of shape function derivatives
        #     NN_r_i = self.stack3r(self.N_r[el, i])
        #     NN_di_i = self.stack3psi(self.N_psi[el, i])

        #     # extract reference state variables
        #     J0i = self.J[el, i]
        #     qwi = self.qw[el, i]
        #     factor_rr = NN_r_i.T @ NN_r_i * J0i * qwi
        #     factor_rdi = NN_r_i.T @ NN_di_i * J0i * qwi
        #     factor_dir = NN_di_i.T @ NN_r_i * J0i * qwi
        #     factor_didi = NN_di_i.T @ NN_di_i * J0i * qwi

        #     # delta r * ddot r
        #     Me[self.rDOF[:, None], self.rDOF] += self.A_rho0 * factor_rr
        #     # delta r * ddot d2
        #     Me[self.rDOF[:, None], self.d2DOF] += self.B_rho0[1] * factor_rdi
        #     # delta r * ddot d3
        #     Me[self.rDOF[:, None], self.d3DOF] += self.B_rho0[2] * factor_rdi

        #     # delta d2 * ddot r
        #     Me[self.d2DOF[:, None], self.rDOF] += self.B_rho0[1] * factor_dir
        #     Me[self.d2DOF[:, None], self.d2DOF] += self.C_rho0[1, 1] * factor_didi
        #     Me[self.d2DOF[:, None], self.d3DOF] += self.C_rho0[1, 2] * factor_didi

        #     # delta d3 * ddot r
        #     Me[self.d3DOF[:, None], self.rDOF] += self.B_rho0[2] * factor_dir
        #     Me[self.d3DOF[:, None], self.d2DOF] += self.C_rho0[2, 1] * factor_didi
        #     Me[self.d3DOF[:, None], self.d3DOF] += self.C_rho0[2, 2] * factor_didi

        return Me

    def __M_coo(self):
        self.__M = Coo((self.nu, self.nu))
        for el in range(self.nelement):
            # extract element degrees of freedom
            elDOF = self.elDOF[el]

            # sparse assemble element mass matrix
            self.__M.extend(self.M_el(el), (self.uDOF[elDOF], self.uDOF[elDOF]))

    def M(self, t, q, coo):
        coo.extend_sparse(self.__M)

    def E_pot(self, t, q):
        E = 0
        for el in range(self.nelement):
            elDOF = self.elDOF[el]
            E += self.E_pot_el(q[elDOF], el)
        return E

    def E_pot_el(self, qe, el):
        Ee = 0

        # extract generalized coordinates for beam centerline and directors
        # in the current and reference configuration
        qe_r = qe[self.rDOF]
        qe_psi = qe[self.psiDOF]

        # since we cannot extract the derivatives of nodal rotation vectors
        # we compute their values on fixed xis's
        psis = np.zeros((self.nnode_psi, 3))
        Rs = np.zeros((self.nnode_psi, 3, 3))
        for i, xii in enumerate(self.xi_el_psi(el)):
            NN_psi_i = self.stack3psi(self.basis_functions_psi(xii)[0])
            psi = NN_psi_i @ qe_psi
            psis[i] = psi

            # evaluate rotation and extract directors
            R = rodriguez(psi)
            d1, d2, d3 = R.T
            Rs[i] = R

        for i in range(self.nquadrature):
            # extract reference state variables
            Ji = self.J[el, i]
            Gamma0_i = self.Gamma0[el, i]
            Kappa0_i = self.Kappa0[el, i]

            # build matrix of shape function derivatives
            NN_r_xii = self.stack3r(self.N_r_xi[el, i])
            NN_psi_i = self.stack3psi(self.N_psi[el, i])
            NN_psi_xii = self.stack3psi(self.N_psi_xi[el, i])

            # Interpolate necessary quantities. The derivatives are evaluated w.r.t.
            # the parameter space \xi and thus need to be transformed later
            r_xi = NN_r_xii @ qe_r

            # build qe's for each director and their derivatives?
            qe_d1 = Rs[:, :, 0].reshape(-1, order="F")  # TODO: Ordering?
            qe_d2 = Rs[:, :, 1].reshape(-1, order="F")  # TODO: Ordering?
            qe_d3 = Rs[:, :, 2].reshape(-1, order="F")  # TODO: Ordering?

            # interpolate these directors using the existing shae functions
            # of the rotation vector
            d1 = NN_psi_i @ qe_d1
            d2 = NN_psi_i @ qe_d2
            d3 = NN_psi_i @ qe_d3
            d1_xi = NN_psi_xii @ qe_d1
            d2_xi = NN_psi_xii @ qe_d2
            d3_xi = NN_psi_xii @ qe_d3

            # compute derivatives w.r.t. the arc lenght parameter s
            r_s = r_xi / Ji
            d1_s = d1_xi / Ji
            d2_s = d2_xi / Ji
            d3_s = d3_xi / Ji

            # axial and shear strains
            Gamma_i = np.array([r_s @ d1, r_s @ d2, r_s @ d3])

            # torsional and flexural strains
            Kappa_i = np.array(
                [
                    0.5 * (d3 @ d2_s - d2 @ d3_s),
                    0.5 * (d1 @ d3_s - d3 @ d1_s),
                    0.5 * (d2 @ d1_s - d1 @ d2_s),
                ]
            )

            # evaluate strain energy function
            Ee += (
                self.material_model.potential(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
                * Ji
                * self.qw[el, i]
            )

        return Ee

    def f_pot(self, t, q):
        f = np.zeros(self.nu)
        for el in range(self.nelement):
            elDOF = self.elDOF[el]
            f[elDOF] += self.f_pot_el(q[elDOF], el)
        return f

    def f_pot_el(self, qe, el):
        return -approx_fprime(qe, lambda qe: self.E_pot_el(qe, el), method="3-point")
        fe = np.zeros(self.nq_elelement)

        # extract generalized coordinates for beam centerline and directors
        # in the current and reference configuration
        qe_r = qe[self.rDOF]
        qe_d1 = qe[self.psiDOF]
        qe_d2 = qe[self.d2DOF]
        qe_d3 = qe[self.d3DOF]

        for i in range(self.nquadrature):
            # build matrix of shape function derivatives
            NN_di_i = self.stack3psi(self.N_psi[el, i])
            NN_r_xii = self.stack3r(self.N_r_xi[el, i])
            NN_di_xii = self.stack3psi(self.N_psi_xi[el, i])

            # extract reference state variables
            J0i = self.J[el, i]
            Gamma0_i = self.Gamma0[el, i]
            Kappa0_i = self.Kappa0[el, i]
            qwi = self.qw[el, i]

            # Interpolate necessary quantities. The derivatives are evaluated w.r.t.
            # the parameter space \xi and thus need to be transformed later
            r_xi = NN_r_xii @ qe_r

            d1 = NN_di_i @ qe_d1
            d1_xi = NN_di_xii @ qe_d1

            d2 = NN_di_i @ qe_d2
            d2_xi = NN_di_xii @ qe_d2

            d3 = NN_di_i @ qe_d3
            d3_xi = NN_di_xii @ qe_d3

            # compute derivatives w.r.t. the arc lenght parameter s
            r_s = r_xi / J0i

            d1_s = d1_xi / J0i
            d2_s = d2_xi / J0i
            d3_s = d3_xi / J0i

            # build rotation matrices
            R = np.vstack((d1, d2, d3)).T

            # axial and shear strains
            Gamma_i = R.T @ r_s

            #################################################################
            # formulation of Harsch2020b
            #################################################################

            # torsional and flexural strains
            Kappa_i = np.array(
                [
                    0.5 * (d3 @ d2_s - d2 @ d3_s),
                    0.5 * (d1 @ d3_s - d3 @ d1_s),
                    0.5 * (d2 @ d1_s - d1 @ d2_s),
                ]
            )

            # compute contact forces and couples from partial derivatives of the strain energy function w.r.t. strain measures
            n1, n2, n3 = self.material_model.n_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            m1, m2, m3 = self.material_model.m_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)

            # quadrature point contribution to element residual
            fe[self.rDOF] -= NN_r_xii.T @ (n1 * d1 + n2 * d2 + n3 * d3) * qwi
            fe[self.psiDOF] -= (
                NN_di_i.T @ (r_xi * n1 + (m2 / 2 * d3_xi - m3 / 2 * d2_xi))
                + NN_di_xii.T @ (m3 / 2 * d2 - m2 / 2 * d3)
            ) * qwi
            fe[self.d2DOF] -= (
                NN_di_i.T @ (r_xi * n2 + (m3 / 2 * d1_xi - m1 / 2 * d3_xi))
                + NN_di_xii.T @ (m1 / 2 * d3 - m3 / 2 * d1)
            ) * qwi
            fe[self.d3DOF] -= (
                NN_di_i.T @ (r_xi * n3 + (m1 / 2 * d2_xi - m2 / 2 * d1_xi))
                + NN_di_xii.T @ (m2 / 2 * d1 - m1 / 2 * d2)
            ) * qwi

        #     #################################################################
        #     # alternative formulation assuming orthogonality of the directors
        #     #################################################################

        #     # torsional and flexural strains
        #     Kappa_i = np.array([d3 @ d2_s, \
        #                         d1 @ d3_s, \
        #                         d2 @ d1_s])

        #     # compute contact forces and couples from partial derivatives of the strain energy function w.r.t. strain measures
        #     n_i = self.material_model.n_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
        #     # n = n_i[0] * d1 + n_i[1] * d2 + n_i[2] * d3
        #     # n = R @ n_i
        #     m_i = self.material_model.m_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
        #     # m = m_i[0] * d1 + m_i[1] * d2 + m_i[2] * d3
        #     # m = R @ m_i

        #     # new version
        #     # quadrature point contribution to element residual
        #     n1, n2, n3 = n_i
        #     m1, m2, m3 = m_i
        #     fe[self.rDOF] -=  NN_r_xii.T @ ( n1 * d1 + n2 * d2 + n3 * d3 ) * qwi # delta r'
        #     fe[self.d1DOF] -= ( NN_di_i.T @ ( r_xi * n1 + m2 * d3_xi ) # delta d1 \
        #                         + NN_di_xii.T @ ( m3 * d2 ) ) * qwi    # delta d1'
        #     fe[self.d2DOF] -= ( NN_di_i.T @ ( r_xi * n2 + m3 * d1_xi ) # delta d2 \
        #                         + NN_di_xii.T @ ( m1 * d3 ) ) * qwi    # delta d2'
        #     fe[self.d3DOF] -= ( NN_di_i.T @ ( r_xi * n3 + m1 * d2_xi ) # delta d3 \
        #                         + NN_di_xii.T @ ( m2 * d1 ) ) * qwi    # delta d3'

        return fe

    def f_pot_q(self, t, q, coo):
        for el in range(self.nelement):
            elDOF = self.elDOF[el]
            Ke = self.f_pot_q_el(q[elDOF], el)

            # sparse assemble element internal stiffness matrix
            coo.extend(Ke, (self.uDOF[elDOF], self.qDOF[elDOF]))

    def f_pot_q_el(self, qe, el):
        return approx_fprime(qe, lambda qe: self.f_pot_el(qe, el), method="2-point")

        Ke = np.zeros((self.nq_elelement, self.nq_elelement))

        # extract generalized coordinates for beam centerline and directors
        # in the current and reference configuration
        qe_r = qe[self.rDOF]
        qe_d1 = qe[self.psiDOF]
        qe_d2 = qe[self.d2DOF]
        qe_d3 = qe[self.d3DOF]

        for i in range(self.nquadrature):
            # build matrix of shape function derivatives
            # NN_r_i = self.stack3r(self.N_r[el, i])
            NN_di_i = self.stack3psi(self.N_psi[el, i])
            NN_r_xii = self.stack3r(self.N_r_xi[el, i])
            NN_di_xii = self.stack3psi(self.N_psi_xi[el, i])

            # extract reference state variables
            J0i = self.J[el, i]
            Gamma0_i = self.Gamma0[el, i]
            Kappa0_i = self.Kappa0[el, i]
            qwi = self.qw[el, i]

            # Interpolate necessary quantities. The derivatives are evaluated w.r.t.
            # the parameter space \xi and thus need to be transformed later
            r_xi = NN_r_xii @ qe_r

            d1 = NN_di_i @ qe_d1
            d1_xi = NN_di_xii @ qe_d1

            d2 = NN_di_i @ qe_d2
            d2_xi = NN_di_xii @ qe_d2

            d3 = NN_di_i @ qe_d3
            d3_xi = NN_di_xii @ qe_d3

            # compute derivatives w.r.t. the arc lenght parameter s
            r_s = r_xi / J0i

            d1_s = d1_xi / J0i
            d2_s = d2_xi / J0i
            d3_s = d3_xi / J0i

            # build rotation matrices
            R = np.vstack((d1, d2, d3)).T

            # axial and shear strains
            Gamma_i = R.T @ r_s

            # derivative of axial and shear strains
            Gamma_j_qr = R.T @ NN_r_xii / J0i
            Gamma_1_qd1 = r_xi @ NN_di_i / J0i
            Gamma_2_qd2 = r_xi @ NN_di_i / J0i
            Gamma_3_qd3 = r_xi @ NN_di_i / J0i

            #################################################################
            # formulation of Harsch2020b
            #################################################################

            # torsional and flexural strains
            Kappa_i = np.array(
                [
                    0.5 * (d3 @ d2_s - d2 @ d3_s),
                    0.5 * (d1 @ d3_s - d3 @ d1_s),
                    0.5 * (d2 @ d1_s - d1 @ d2_s),
                ]
            )

            # derivative of torsional and flexural strains
            kappa_1_qe_d1 = np.zeros(3 * self.nnodes_element_psi)
            kappa_1_qe_d2 = 0.5 * (d3 @ NN_di_xii - d3_xi @ NN_di_i) / J0i
            kappa_1_qe_d3 = 0.5 * (d2_xi @ NN_di_i - d2 @ NN_di_xii) / J0i

            kappa_2_qe_d1 = 0.5 * (d3_xi @ NN_di_i - d3 @ NN_di_xii) / J0i
            kappa_2_qe_d2 = np.zeros(3 * self.nnodes_element_psi)
            kappa_2_qe_d3 = 0.5 * (d1 @ NN_di_xii - d1_xi @ NN_di_i) / J0i

            kappa_3_qe_d1 = 0.5 * (d2 @ NN_di_xii - d2_xi @ NN_di_i) / J0i
            kappa_3_qe_d2 = 0.5 * (d1_xi @ NN_di_i - d1 @ NN_di_xii) / J0i
            kappa_3_qe_d3 = np.zeros(3 * self.nnodes_element_psi)

            kappa_j_qe_d1 = np.vstack((kappa_1_qe_d1, kappa_2_qe_d1, kappa_3_qe_d1))
            kappa_j_qe_d2 = np.vstack((kappa_1_qe_d2, kappa_2_qe_d2, kappa_3_qe_d2))
            kappa_j_qe_d3 = np.vstack((kappa_1_qe_d3, kappa_2_qe_d3, kappa_3_qe_d3))

            # compute contact forces and couples from partial derivatives of the strain energy function w.r.t. strain measures
            n1, n2, n3 = self.material_model.n_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            n_i_Gamma_j = self.material_model.n_i_Gamma_j(
                Gamma_i, Gamma0_i, Kappa_i, Kappa0_i
            )
            n_i_Kappa_j = self.material_model.n_i_K_j(
                Gamma_i, Gamma0_i, Kappa_i, Kappa0_i
            )
            n1_qr, n2_qr, n3_qr = n_i_Gamma_j @ Gamma_j_qr

            n1_qd1, n2_qd1, n3_qd1 = (
                np.outer(n_i_Gamma_j[0], Gamma_1_qd1) + n_i_Kappa_j @ kappa_j_qe_d1
            )
            n1_qd2, n2_qd2, n3_qd2 = (
                np.outer(n_i_Gamma_j[1], Gamma_2_qd2) + n_i_Kappa_j @ kappa_j_qe_d2
            )
            n1_qd3, n2_qd3, n3_qd3 = (
                np.outer(n_i_Gamma_j[2], Gamma_3_qd3) + n_i_Kappa_j @ kappa_j_qe_d3
            )

            m1, m2, m3 = self.material_model.m_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            m_i_Gamma_j = self.material_model.m_i_Gamma_j(
                Gamma_i, Gamma0_i, Kappa_i, Kappa0_i
            )
            m_i_Kappa_j = self.material_model.m_i_K_j(
                Gamma_i, Gamma0_i, Kappa_i, Kappa0_i
            )

            m1_qr, m2_qr, m3_qr = m_i_Gamma_j @ Gamma_j_qr
            m1_qd1, m2_qd1, m3_qd1 = (
                np.outer(m_i_Gamma_j[0], Gamma_1_qd1) + m_i_Kappa_j @ kappa_j_qe_d1
            )
            m1_qd2, m2_qd2, m3_qd2 = (
                np.outer(m_i_Gamma_j[1], Gamma_2_qd2) + m_i_Kappa_j @ kappa_j_qe_d2
            )
            m1_qd3, m2_qd3, m3_qd3 = (
                np.outer(m_i_Gamma_j[2], Gamma_3_qd3) + m_i_Kappa_j @ kappa_j_qe_d3
            )

            Ke[self.rDOF[:, None], self.rDOF] -= (
                NN_r_xii.T
                @ (np.outer(d1, n1_qr) + np.outer(d2, n2_qr) + np.outer(d3, n3_qr))
                * qwi
            )
            Ke[self.rDOF[:, None], self.psiDOF] -= (
                NN_r_xii.T
                @ (
                    np.outer(d1, n1_qd1)
                    + np.outer(d2, n2_qd1)
                    + np.outer(d3, n3_qd1)
                    + n1 * NN_di_i
                )
                * qwi
            )
            Ke[self.rDOF[:, None], self.d2DOF] -= (
                NN_r_xii.T
                @ (
                    np.outer(d1, n1_qd2)
                    + np.outer(d2, n2_qd2)
                    + np.outer(d3, n3_qd2)
                    + n2 * NN_di_i
                )
                * qwi
            )
            Ke[self.rDOF[:, None], self.d3DOF] -= (
                NN_r_xii.T
                @ (
                    np.outer(d1, n1_qd3)
                    + np.outer(d2, n2_qd3)
                    + np.outer(d3, n3_qd3)
                    + n3 * NN_di_i
                )
                * qwi
            )

            Ke[self.psiDOF[:, None], self.rDOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n1_qr)
                    + n1 * NN_r_xii
                    + np.outer(0.5 * d3_xi, m2_qr)
                    - np.outer(0.5 * d2_xi, m3_qr)
                )
                * qwi
                + NN_di_xii.T
                @ (np.outer(0.5 * d2, m3_qr) - np.outer(0.5 * d3, m2_qr))
                * qwi
            )
            Ke[self.psiDOF[:, None], self.psiDOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n1_qd1)
                    + np.outer(0.5 * d3_xi, m2_qd1)
                    - np.outer(0.5 * d2_xi, m3_qd1)
                )
                * qwi
                + NN_di_xii.T
                @ (np.outer(0.5 * d2, m3_qd1) - np.outer(0.5 * d3, m2_qd1))
                * qwi
            )
            Ke[self.psiDOF[:, None], self.d2DOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n1_qd2)
                    + np.outer(0.5 * d3_xi, m2_qd2)
                    - np.outer(0.5 * d2_xi, m3_qd2)
                    - 0.5 * m3 * NN_di_xii
                )
                * qwi
                + NN_di_xii.T
                @ (
                    np.outer(0.5 * d2, m3_qd2)
                    + 0.5 * m3 * NN_di_i
                    - np.outer(0.5 * d3, m2_qd2)
                )
                * qwi
            )
            Ke[self.psiDOF[:, None], self.d3DOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n1_qd3)
                    + np.outer(0.5 * d3_xi, m2_qd3)
                    + 0.5 * m2 * NN_di_xii
                    - np.outer(0.5 * d2_xi, m3_qd3)
                )
                * qwi
                + NN_di_xii.T
                @ (
                    np.outer(0.5 * d2, m3_qd3)
                    - np.outer(0.5 * d3, m2_qd3)
                    - 0.5 * m2 * NN_di_i
                )
                * qwi
            )

            Ke[self.d2DOF[:, None], self.rDOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n2_qr)
                    + n2 * NN_r_xii
                    + np.outer(0.5 * d1_xi, m3_qr)
                    - np.outer(0.5 * d3_xi, m1_qr)
                )
                * qwi
                + NN_di_xii.T
                @ (np.outer(0.5 * d3, m1_qr) - np.outer(0.5 * d1, m3_qr))
                * qwi
            )
            Ke[self.d2DOF[:, None], self.psiDOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n2_qd1)
                    + np.outer(0.5 * d1_xi, m3_qd1)
                    + 0.5 * m3 * NN_di_xii
                    - np.outer(0.5 * d3_xi, m1_qd1)
                )
                * qwi
                + NN_di_xii.T
                @ (
                    np.outer(0.5 * d3, m1_qd1)
                    - np.outer(0.5 * d1, m3_qd1)
                    - 0.5 * m3 * NN_di_i
                )
                * qwi
            )
            Ke[self.d2DOF[:, None], self.d2DOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n2_qd2)
                    + np.outer(0.5 * d1_xi, m3_qd2)
                    - np.outer(0.5 * d3_xi, m1_qd2)
                )
                * qwi
                + NN_di_xii.T
                @ (np.outer(0.5 * d3, m1_qd2) - np.outer(0.5 * d1, m3_qd2))
                * qwi
            )
            Ke[self.d2DOF[:, None], self.d3DOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n2_qd3)
                    + np.outer(0.5 * d1_xi, m3_qd3)
                    - np.outer(0.5 * d3_xi, m1_qd3)
                    - 0.5 * m1 * NN_di_xii
                )
                * qwi
                + NN_di_xii.T
                @ (
                    np.outer(0.5 * d3, m1_qd3)
                    + 0.5 * m1 * NN_di_i
                    - np.outer(0.5 * d1, m3_qd3)
                )
                * qwi
            )

            Ke[self.d3DOF[:, None], self.rDOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n3_qr)
                    + n3 * NN_r_xii
                    + np.outer(0.5 * d2_xi, m1_qr)
                    - np.outer(0.5 * d1_xi, m2_qr)
                )
                * qwi
                + NN_di_xii.T
                @ (np.outer(0.5 * d1, m2_qr) - np.outer(0.5 * d2, m1_qr))
                * qwi
            )
            Ke[self.d3DOF[:, None], self.psiDOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n3_qd1)
                    + np.outer(0.5 * d2_xi, m1_qd1)
                    - np.outer(0.5 * d1_xi, m2_qd1)
                    - 0.5 * m2 * NN_di_xii
                )
                * qwi
                + NN_di_xii.T
                @ (
                    np.outer(0.5 * d1, m2_qd1)
                    + 0.5 * m2 * NN_di_i
                    - np.outer(0.5 * d2, m1_qd1)
                )
                * qwi
            )
            Ke[self.d3DOF[:, None], self.d2DOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n3_qd2)
                    + np.outer(0.5 * d2_xi, m1_qd2)
                    + 0.5 * m1 * NN_di_xii
                    - np.outer(0.5 * d1_xi, m2_qd2)
                )
                * qwi
                + NN_di_xii.T
                @ (
                    np.outer(0.5 * d1, m2_qd2)
                    - np.outer(0.5 * d2, m1_qd2)
                    - 0.5 * m1 * NN_di_i
                )
                * qwi
            )
            Ke[self.d3DOF[:, None], self.d3DOF] -= (
                NN_di_i.T
                @ (
                    np.outer(r_xi, n3_qd3)
                    + np.outer(0.5 * d2_xi, m1_qd3)
                    - np.outer(0.5 * d1_xi, m2_qd3)
                )
                * qwi
                + NN_di_xii.T
                @ (np.outer(0.5 * d1, m2_qd3) - np.outer(0.5 * d2, m1_qd3))
                * qwi
            )

            # #################################################################
            # # alternative formulation assuming orthogonality of the directors
            # #################################################################

            # # torsional and flexural strains
            # Kappa_i = np.array([d3 @ d2_s, \
            #                     d1 @ d3_s, \
            #                     d2 @ d1_s])

            # # derivative of torsional and flexural strains
            # kappa_1_qe_d1 = np.zeros(3 * self.nn_el_di)
            # kappa_1_qe_d2 = d3 @ NN_di_xii / J0i
            # kappa_1_qe_d3 = d2_s @ NN_di_i

            # kappa_2_qe_d1 = d3_s @ NN_di_i
            # kappa_2_qe_d2 = np.zeros(3 * self.nn_el_di)
            # kappa_2_qe_d3 = d1 @ NN_di_xii / J0i

            # kappa_3_qe_d1 = d2 @ NN_di_xii / J0i
            # kappa_3_qe_d2 = d1_s @ NN_di_i
            # kappa_3_qe_d3 = np.zeros(3 * self.nn_el_di)

            # kappa_j_qe_d1 = np.vstack((kappa_1_qe_d1, kappa_2_qe_d1, kappa_3_qe_d1))
            # kappa_j_qe_d2 = np.vstack((kappa_1_qe_d2, kappa_2_qe_d2, kappa_3_qe_d2))
            # kappa_j_qe_d3 = np.vstack((kappa_1_qe_d3, kappa_2_qe_d3, kappa_3_qe_d3))

            # # compute contact forces and couples from partial derivatives of the strain energy function w.r.t. strain measures
            # n1, n2, n3 = self.material_model.n_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            # n_i_Gamma_i_j = self.material_model.n_i_Gamma_i_j(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            # n_i_Kappa_i_j = self.material_model.n_i_Kappa_i_j(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            # n1_qr, n2_qr, n3_qr = n_i_Gamma_i_j @ Gamma_j_qr

            # n1_qd1, n2_qd1, n3_qd1 = np.outer(n_i_Gamma_i_j[0], Gamma_1_qd1) + n_i_Kappa_i_j @ kappa_j_qe_d1
            # n1_qd2, n2_qd2, n3_qd2 = np.outer(n_i_Gamma_i_j[1], Gamma_2_qd2) + n_i_Kappa_i_j @ kappa_j_qe_d2
            # n1_qd3, n2_qd3, n3_qd3 = np.outer(n_i_Gamma_i_j[2], Gamma_3_qd3) + n_i_Kappa_i_j @ kappa_j_qe_d3

            # m1, m2, m3 = self.material_model.m_i(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            # m_i_Gamma_i_j = self.material_model.m_i_Gamma_i_j(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)
            # m_i_Kappa_i_j = self.material_model.m_i_Kappa_i_j(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i)

            # m1_qr, m2_qr, m3_qr = m_i_Gamma_i_j @ Gamma_j_qr
            # m1_qd1, m2_qd1, m3_qd1 = np.outer(m_i_Gamma_i_j[0], Gamma_1_qd1) + m_i_Kappa_i_j @ kappa_j_qe_d1
            # m1_qd2, m2_qd2, m3_qd2 = np.outer(m_i_Gamma_i_j[1], Gamma_2_qd2) + m_i_Kappa_i_j @ kappa_j_qe_d2
            # m1_qd3, m2_qd3, m3_qd3 = np.outer(m_i_Gamma_i_j[2], Gamma_3_qd3) + m_i_Kappa_i_j @ kappa_j_qe_d3

            # # quadrature point contribution to element stiffness matrix

            # # fe[self.rDOF] -=  NN_r_xii.T @ ( n1 * d1 + n2 * d2 + n3 * d3 ) * qwi # delta r'
            # Ke[self.rDOF[:, None], self.rDOF] -= NN_r_xii.T @ (np.outer(d1, n1_qr) + np.outer(d2, n2_qr) + np.outer(d3, n3_qr)) * qwi
            # Ke[self.rDOF[:, None], self.d1DOF] -= NN_r_xii.T @ (np.outer(d1, n1_qd1) + np.outer(d2, n2_qd1) + np.outer(d3, n3_qd1) + n1 * NN_di_i) * qwi
            # Ke[self.rDOF[:, None], self.d2DOF] -= NN_r_xii.T @ (np.outer(d1, n1_qd2) + np.outer(d2, n2_qd2) + np.outer(d3, n3_qd2) + n2 * NN_di_i) * qwi
            # Ke[self.rDOF[:, None], self.d3DOF] -= NN_r_xii.T @ (np.outer(d1, n1_qd3) + np.outer(d2, n2_qd3) + np.outer(d3, n3_qd3) + n3 * NN_di_i) * qwi

            # # fe[self.d1DOF] -= ( NN_di_i.T @ ( r_xi * n1 + m2 * d3_xi ) # delta d1 \
            # #                     + NN_di_xii.T @ ( m3 * d2 ) ) * qwi    # delta d1'
            # Ke[self.d1DOF[:, None], self.rDOF] -= NN_di_i.T @ (np.outer(r_xi, n1_qr) + n1 * NN_r_xii + np.outer(d3_xi, m2_qr)) * qwi \
            #                                       + NN_di_xii.T @ np.outer(d2, m3_qr) * qwi
            # Ke[self.d1DOF[:, None], self.d1DOF] -= NN_di_i.T @ (np.outer(r_xi, n1_qd1) + np.outer(d3_xi, m2_qd1)) * qwi \
            #                                        + NN_di_xii.T @ np.outer(d2, m3_qd1) * qwi
            # Ke[self.d1DOF[:, None], self.d2DOF] -= NN_di_i.T @ (np.outer(r_xi, n1_qd2) + np.outer(d3_xi, m2_qd2)) * qwi \
            #                                        + NN_di_xii.T @ (np.outer(d2, m3_qd2) + m3 * NN_di_i) * qwi
            # Ke[self.d1DOF[:, None], self.d3DOF] -= NN_di_i.T @ (np.outer(r_xi, n1_qd3) + np.outer(d3_xi, m2_qd3) + m2 * NN_di_xii) * qwi \
            #                                        + NN_di_xii.T @ np.outer(d2, m3_qd3) * qwi

            # # fe[self.d2DOF] -= ( NN_di_i.T @ ( r_xi * n2 + m3 * d1_xi ) # delta d2 \
            # #                     + NN_di_xii.T @ ( m1 * d3 ) ) * qwi    # delta d2'
            # Ke[self.d2DOF[:, None], self.rDOF] -= NN_di_i.T @ (np.outer(r_xi, n2_qr) + n2 * NN_r_xii + np.outer(d1_xi, m3_qr)) * qwi \
            #                                       + NN_di_xii.T @ np.outer(d3, m1_qr) * qwi
            # Ke[self.d2DOF[:, None], self.d1DOF] -= NN_di_i.T @ (np.outer(r_xi, n2_qd1) + np.outer(d1_xi, m3_qd1) + m3 * NN_di_xii) * qwi \
            #                                        + NN_di_xii.T @ np.outer(d3, m1_qd1) * qwi
            # Ke[self.d2DOF[:, None], self.d2DOF] -= NN_di_i.T @ (np.outer(r_xi, n2_qd2) + np.outer(d1_xi, m3_qd2)) * qwi \
            #                                        + NN_di_xii.T @ np.outer(d3, m1_qd2) * qwi
            # Ke[self.d2DOF[:, None], self.d3DOF] -= NN_di_i.T @ (np.outer(r_xi, n2_qd3) + np.outer(d1_xi, m3_qd3)) * qwi \
            #                                        + NN_di_xii.T @ (np.outer(d3, m1_qd3) + m1 * NN_di_i) * qwi

            # # fe[self.d3DOF] -= ( NN_di_i.T @ ( r_xi * n3 + m1 * d2_xi ) # delta d3 \
            # #                     + NN_di_xii.T @ ( m2 * d1 ) ) * qwi    # delta d3'
            # Ke[self.d3DOF[:, None], self.rDOF] -= NN_di_i.T @ (np.outer(r_xi, n3_qr) + n3 * NN_r_xii + np.outer(d2_xi, m1_qr)) * qwi \
            #                                       + NN_di_xii.T @ np.outer(d1, m2_qr) * qwi
            # Ke[self.d3DOF[:, None], self.d1DOF] -= NN_di_i.T @ (np.outer(r_xi, n3_qd1) + np.outer(d2_xi, m1_qd1)) * qwi \
            #                                        + NN_di_xii.T @ (np.outer(d1, m2_qd1) + m2 * NN_di_i) * qwi
            # Ke[self.d3DOF[:, None], self.d2DOF] -= NN_di_i.T @ (np.outer(r_xi, n3_qd2) + np.outer(d2_xi, m1_qd2) + m1 * NN_di_xii) * qwi \
            #                                        + NN_di_xii.T @ np.outer(d1, m2_qd2) * qwi
            # Ke[self.d3DOF[:, None], self.d3DOF] -= NN_di_i.T @ (np.outer(r_xi, n3_qd3) + np.outer(d2_xi, m1_qd3)) * qwi \
            #                                        + NN_di_xii.T @ np.outer(d1, m2_qd3) * qwi

        return Ke

        # Ke_num = Numerical_derivative(lambda t, qe: self.f_pot_el(qe, el), order=2)._x(0, qe)
        # diff = Ke_num - Ke
        # error = np.max(np.abs(diff))
        # print(f'max error f_pot_q_el: {error}')

        # print(f'diff[self.rDOF[:, None], self.rDOF]: {np.max(np.abs(diff[self.rDOF[:, None], self.rDOF]))}')
        # print(f'diff[self.rDOF[:, None], self.d1DOF]: {np.max(np.abs(diff[self.rDOF[:, None], self.d1DOF]))}')
        # print(f'diff[self.rDOF[:, None], self.d2DOF]: {np.max(np.abs(diff[self.rDOF[:, None], self.d2DOF]))}')
        # print(f'diff[self.rDOF[:, None], self.d3DOF]: {np.max(np.abs(diff[self.rDOF[:, None], self.d3DOF]))}')

        # print(f'diff[self.d1DOF[:, None], self.rDOF]: {np.max(np.abs(diff[self.d1DOF[:, None], self.rDOF]))}')
        # print(f'diff[self.d1DOF[:, None], self.d1DOF]: {np.max(np.abs(diff[self.d1DOF[:, None], self.d1DOF]))}')
        # print(f'diff[self.d1DOF[:, None], self.d2DOF]: {np.max(np.abs(diff[self.d1DOF[:, None], self.d2DOF]))}')
        # print(f'diff[self.d1DOF[:, None], self.d3DOF]: {np.max(np.abs(diff[self.d1DOF[:, None], self.d3DOF]))}')

        # print(f'diff[self.d2DOF[:, None], self.rDOF]: {np.max(np.abs(diff[self.d2DOF[:, None], self.rDOF]))}')
        # print(f'diff[self.d2DOF[:, None], self.d1DOF]: {np.max(np.abs(diff[self.d2DOF[:, None], self.d1DOF]))}')
        # print(f'diff[self.d2DOF[:, None], self.d2DOF]: {np.max(np.abs(diff[self.d2DOF[:, None], self.d2DOF]))}')
        # print(f'diff[self.d2DOF[:, None], self.d3DOF]: {np.max(np.abs(diff[self.d2DOF[:, None], self.d3DOF]))}')

        # print(f'diff[self.d3DOF[:, None], self.rDOF]: {np.max(np.abs(diff[self.d3DOF[:, None], self.rDOF]))}')
        # print(f'diff[self.d3DOF[:, None], self.d1DOF]: {np.max(np.abs(diff[self.d3DOF[:, None], self.d1DOF]))}')
        # print(f'diff[self.d3DOF[:, None], self.d2DOF]: {np.max(np.abs(diff[self.d3DOF[:, None], self.d2DOF]))}')
        # print(f'diff[self.d3DOF[:, None], self.d3DOF]: {np.max(np.abs(diff[self.d3DOF[:, None], self.d3DOF]))}')

        # return Ke_num

    #########################################
    # kinematic equation
    #########################################
    def q_dot(self, t, q, u):
        return u

    def B(self, t, q, coo):
        coo.extend_diag(np.ones(self.nq), (self.qDOF, self.uDOF))

    def q_ddot(self, t, q, u, u_dot):
        return u_dot

    ####################################################
    # interactions with other bodies and the environment
    ####################################################
    # TODO: optimized implementation for boundaries
    def elDOF_P(self, frame_ID):
        xi = frame_ID[0]
        # if xi == 0:
        #     return self.elDOF[0]
        # elif xi == 1:
        #     return self.elDOF[-1]
        # else:
        #     el = np.where(xi >= self.element_span)[0][-1]
        #     return self.elDOF[el]
        # el = np.where(xi >= self.element_span)[0][-1]
        el = self.element_number(xi)
        return self.elDOF[el]

    def qDOF_P(self, frame_ID):
        return self.elDOF_P(frame_ID)

    def uDOF_P(self, frame_ID):
        return self.elDOF_P(frame_ID)

    # TODO: optimized implementation for boundaries
    def r_OP(self, t, q, frame_ID, K_r_SP=np.zeros(3)):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_r(frame_ID[0])
        NN = self.stack3r(N)
        return NN @ q[self.rDOF] + self.A_IK(t, q, frame_ID=frame_ID) @ K_r_SP

    # TODO: optimized implementation for boundaries
    def r_OP_q(self, t, q, frame_ID, K_r_SP=np.zeros(3)):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_r(frame_ID[0])
        NN = self.stack3r(N)

        r_OP_q = np.zeros((3, self.nq_elelement))
        r_OP_q[:, self.rDOF] = NN
        r_OP_q += np.einsum("ijk,j->ik", self.A_IK_q(t, q, frame_ID=frame_ID), K_r_SP)

        # tmp = np.einsum("ijk,j->ik", self.A_IK_q(t, q, frame_ID=frame_ID), K_r_SP)
        # r_OP_q[:, self.d1DOF] = tmp[:, self.d1DOF]
        # r_OP_q[:, self.d2DOF] = tmp[:, self.d2DOF]
        # r_OP_q[:, self.d3DOF] = tmp[:, self.d3DOF]
        return r_OP_q

        # r_OP_q_num = Numerical_derivative(lambda t, q: self.r_OP(t, q, frame_ID=frame_ID, K_r_SP=K_r_SP))._x(t, q)
        # error = np.max(np.abs(r_OP_q_num - r_OP_q))
        # print(f'error in r_OP_q: {error}')
        # return r_OP_q_num

    # TODO: optimized implementation for boundaries
    def A_IK(self, t, q, frame_ID):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_psi(frame_ID[0])
        NN = self.stack3psi(N)

        d1 = NN @ q[self.psiDOF]
        d2 = NN @ q[self.d2DOF]
        d3 = NN @ q[self.d3DOF]
        return np.vstack((d1, d2, d3)).T

    # TODO: optimized implementation for boundaries
    def A_IK_q(self, t, q, frame_ID):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_psi(frame_ID[0])
        NN = self.stack3psi(N)

        A_IK_q = np.zeros((3, 3, self.nq_elelement))
        A_IK_q[:, 0, self.psiDOF] = NN
        A_IK_q[:, 1, self.d2DOF] = NN
        A_IK_q[:, 2, self.d3DOF] = NN
        return A_IK_q

        # A_IK_q_num =  Numerical_derivative(lambda t, q: self.A_IK(t, q, frame_ID=frame_ID))._x(t, q)
        # error = np.linalg.norm(A_IK_q - A_IK_q_num)
        # print(f'error in A_IK_q: {error}')
        # return A_IK_q_num

    # TODO: optimized implementation for boundaries
    def v_P(self, t, q, u, frame_ID, K_r_SP=np.zeros(3)):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_r(frame_ID[0])
        NN = self.stack3r(N)

        # v_P1 = NN @ u[self.rDOF] + self.A_IK(t, q, frame_ID) @ cross3(self.K_Omega(t, q, u, frame_ID=frame_ID), K_r_SP)
        v_P2 = NN @ u[self.rDOF] + self.A_IK(t, u, frame_ID) @ K_r_SP
        # print(v_P1 - v_P2)
        return v_P2

    def v_P_q(self, t, q, u, frame_ID, K_r_SP=np.zeros(3)):
        return np.zeros((3, self.nq_elelement))

    def J_P(self, t, q, frame_ID, K_r_SP=np.zeros(3)):
        return self.r_OP_q(t, q, frame_ID=frame_ID, K_r_SP=K_r_SP)

    def J_P_q(self, t, q, frame_ID=None, K_r_SP=np.zeros(3)):
        return np.zeros((3, self.nq_elelement, self.nq_elelement))

    # TODO: optimized implementation for boundaries
    def a_P(self, t, q, u, u_dot, frame_ID, K_r_SP=np.zeros(3)):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_r(frame_ID[0])
        NN = self.stack3r(N)

        # K_Omega = self.K_Omega(t, q, u, frame_ID=frame_ID)
        # K_Psi = self.K_Psi(t, q, u, u_dot, frame_ID=frame_ID)
        # a_P1 = NN @ u_dot[self.rDOF] + self.A_IK(t, q, frame_ID=frame_ID) @ (cross3(K_Psi, K_r_SP) + cross3(K_Omega, cross3(K_Omega, K_r_SP)))
        a_P2 = NN @ u_dot[self.rDOF] + self.A_IK(t, u_dot, frame_ID=frame_ID) @ K_r_SP
        # print(a_P1 - a_P2)
        return a_P2

    def a_P_q(self, t, q, u, u_dot, frame_ID, K_r_SP=None):
        return np.zeros((3, self.nq_elelement))

    def a_P_u(self, t, q, u, u_dot, frame_ID, K_r_SP=None):
        return np.zeros((3, self.nq_elelement))

    # TODO: optimized implementation for boundaries
    def K_Omega(self, t, q, u, frame_ID):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_psi(frame_ID[0])
        NN = self.stack3psi(N)

        d1 = NN @ q[self.psiDOF]
        d2 = NN @ q[self.d2DOF]
        d3 = NN @ q[self.d3DOF]
        A_IK = np.vstack((d1, d2, d3)).T

        d1_dot = NN @ u[self.psiDOF]
        d2_dot = NN @ u[self.d2DOF]
        d3_dot = NN @ u[self.d3DOF]
        A_IK_dot = np.vstack((d1_dot, d2_dot, d3_dot)).T

        K_Omega_tilde = A_IK.T @ A_IK_dot
        return skew2ax(K_Omega_tilde)

    # TODO: optimized implementation for boundaries
    def K_J_R(self, t, q, frame_ID):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_psi(frame_ID[0])
        NN = self.stack3psi(N)

        d1 = NN @ q[self.psiDOF]
        d2 = NN @ q[self.d2DOF]
        d3 = NN @ q[self.d3DOF]
        A_IK = np.vstack((d1, d2, d3)).T

        K_Omega_tilde_Omega_tilde = skew2ax_A()

        K_J_R = np.zeros((3, self.nq_elelement))
        K_J_R[:, self.psiDOF] = K_Omega_tilde_Omega_tilde[0] @ A_IK.T @ NN
        K_J_R[:, self.d2DOF] = K_Omega_tilde_Omega_tilde[1] @ A_IK.T @ NN
        K_J_R[:, self.d3DOF] = K_Omega_tilde_Omega_tilde[2] @ A_IK.T @ NN
        return K_J_R

        # K_J_R_num = Numerical_derivative(lambda t, q, u: self.K_Omega(t, q, u, frame_ID=frame_ID), order=2)._y(t, q, np.zeros_like(q))
        # diff = K_J_R_num - K_J_R
        # diff_error = diff
        # error = np.linalg.norm(diff_error)
        # print(f'error K_J_R: {error}')
        # return K_J_R_num

    # TODO: optimized implementation for boundaries
    def K_J_R_q(self, t, q, frame_ID):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_psi(frame_ID[0])
        NN = self.stack3psi(N)

        A_IK_q = np.zeros((3, 3, self.nq_elelement))
        A_IK_q[:, 0, self.psiDOF] = NN
        A_IK_q[:, 1, self.d2DOF] = NN
        A_IK_q[:, 2, self.d3DOF] = NN

        K_Omega_tilde_Omega_tilde = skew2ax_A()
        tmp = np.einsum("jil,jk->ikl", A_IK_q, NN)

        K_J_R_q = np.zeros((3, self.nq_elelement, self.nq_elelement))
        K_J_R_q[:, self.psiDOF] = np.einsum(
            "ij,jkl->ikl", K_Omega_tilde_Omega_tilde[0], tmp
        )
        K_J_R_q[:, self.d2DOF] = np.einsum(
            "ij,jkl->ikl", K_Omega_tilde_Omega_tilde[1], tmp
        )
        K_J_R_q[:, self.d3DOF] = np.einsum(
            "ij,jkl->ikl", K_Omega_tilde_Omega_tilde[2], tmp
        )
        return K_J_R_q

        # K_J_R_q_num = Numerical_derivative(lambda t, q: self.K_J_R(t, q, frame_ID))._x(t, q)
        # error = np.max(np.abs(K_J_R_q_num - K_J_R_q))
        # print(f'error K_J_R_q: {error}')
        # return K_J_R_q_num

    # TODO: optimized implementation for boundaries
    def K_Psi(self, t, q, u, u_dot, frame_ID):
        # xi = frame_ID[0]
        # if xi == 0:
        #     NN = self.N_bdry[0]
        # elif xi == 1:
        #     NN = self.N_bdry[-1]
        # else:
        #     N, _ = self.basis_functions(frame_ID[0])
        #     NN = self.stack3r(N)
        N, _ = self.basis_functions_psi(frame_ID[0])
        NN = self.stack3psi(N)

        d1 = NN @ q[self.psiDOF]
        d2 = NN @ q[self.d2DOF]
        d3 = NN @ q[self.d3DOF]
        A_IK = np.vstack((d1, d2, d3)).T

        d1_dot = NN @ u[self.psiDOF]
        d2_dot = NN @ u[self.d2DOF]
        d3_dot = NN @ u[self.d3DOF]
        A_IK_dot = np.vstack((d1_dot, d2_dot, d3_dot)).T

        d1_ddot = NN @ u_dot[self.psiDOF]
        d2_ddot = NN @ u_dot[self.d2DOF]
        d3_ddot = NN @ u_dot[self.d3DOF]
        A_IK_ddot = np.vstack((d1_ddot, d2_ddot, d3_ddot)).T

        K_Psi_tilde = A_IK_dot.T @ A_IK_dot + A_IK.T @ A_IK_ddot
        return skew2ax(K_Psi_tilde)

    ####################################################
    # body force
    ####################################################
    # def body_force_el(self, force, t, el):
    def distributed_force1D_el(self, force, t, el):
        fe = np.zeros(self.nq_elelement)
        for i in range(self.nquadrature):
            NNi = self.stack3r(self.N_r[el, i])
            fe[self.rDOF] += (
                NNi.T @ force(t, self.qp[el, i]) * self.J[el, i] * self.qw[el, i]
            )
        return fe

    # def body_force(self, t, q, force):
    def distributed_force1D(self, t, q, force):
        f = np.zeros(self.nq)
        for el in range(self.nelement):
            # f[self.elDOF[el]] += self.body_force_el(force, t, el)
            f[self.elDOF[el]] += self.distributed_force1D_el(force, t, el)
        return f

    # def body_force_q(self, t, q, coo, force):
    def distributed_force1D_q(self, t, q, coo, force):
        pass

    ####################################################
    # visualization
    ####################################################
    def nodes(self, q):
        return q[self.qDOF][: 3 * self.nnode_r].reshape(3, -1)

    def centerline(self, q, n=100):
        q_body = q[self.qDOF]
        r = []
        for xi in np.linspace(0, 1, n):
            frame_ID = (xi,)
            qp = q_body[self.qDOF_P(frame_ID)]
            r.append(self.r_OP(1, qp, frame_ID))
        return np.array(r).T

    def frames(self, q, n=10):
        q_body = q[self.qDOF]
        r = []
        d1 = []
        d2 = []
        d3 = []

        for xi in np.linspace(0, 1, n):
            frame_ID = (xi,)
            qp = q_body[self.qDOF_P(frame_ID)]
            r.append(self.r_OP(1, qp, frame_ID))

            d1i, d2i, d3i = self.A_IK(1, qp, frame_ID).T
            d1.extend([d1i])
            d2.extend([d2i])
            d3.extend([d3i])

        return np.array(r).T, np.array(d1).T, np.array(d2).T, np.array(d3).T

    def plot_centerline(self, ax, q, n=100, color="black"):
        ax.plot(*self.nodes(q), linestyle="dashed", marker="o", color=color)
        ax.plot(*self.centerline(q, n=n), linestyle="solid", color=color)

    def plot_frames(self, ax, q, n=10, length=1):
        r, d1, d2, d3 = self.frames(q, n=n)
        ax.quiver(*r, *d1, color="red", length=length)
        ax.quiver(*r, *d2, color="green", length=length)
        ax.quiver(*r, *d3, color="blue", length=length)

    ############
    # vtk export
    ############
    def post_processing_vtk_volume_circle(self, t, q, filename, R, binary=False):
        # This is mandatory, otherwise we cannot construct the 3D continuum without L2 projection!
        assert (
            self.polynomial_degree_r == self.polynomial_degree_psi
        ), "Not implemented for mixed polynomial degrees"

        # rearrange generalized coordinates from solver ordering to Piegl's Pw 3D array
        nn_xi = self.nelement + self.polynomial_degree_r
        nEl_eta = 1
        nEl_zeta = 4
        # see Cotrell2009 Section 2.4.2
        # TODO: Maybe eta and zeta have to be interchanged
        polynomial_degree_eta = 1
        polynomial_degree_zeta = 2
        nn_eta = nEl_eta + polynomial_degree_eta
        nn_zeta = nEl_zeta + polynomial_degree_zeta

        # # TODO: We do the hard coded case for rectangular cross section here, but this has to be extended to the circular cross section case too!
        # as_ = np.linspace(-a/2, a/2, num=nn_eta, endpoint=True)
        # bs_ = np.linspace(-b/2, b/2, num=nn_eta, endpoint=True)

        circle_points = (
            0.5
            * R
            * np.array(
                [
                    [1, 0, 0],
                    [1, 1, 0],
                    [0, 1, 0],
                    [-1, 1, 0],
                    [-1, 0, 0],
                    [-1, -1, 0],
                    [0, -1, 0],
                    [1, -1, 0],
                    [1, 0, 0],
                ],
                dtype=float,
            )
        )

        Pw = np.zeros((nn_xi, nn_eta, nn_zeta, 3))
        for i in range(self.nnode_r):
            qr = q[self.nodalDOF_r[i]]
            q_di = q[self.nodalDOF_psi[i]]
            A_IK = q_di.reshape(3, 3, order="F")  # TODO: Check this!

            for k, point in enumerate(circle_points):
                # Note: eta index is always 0!
                Pw[i, 0, k] = qr + A_IK @ point

        if self.basis == "B-spline":
            knot_vector_eta = KnotVector(polynomial_degree_eta, nEl_eta)
            knot_vector_zeta = KnotVector(polynomial_degree_zeta, nEl_zeta)
        elif self.basis == "lagrange":
            knot_vector_eta = Node_vector(polynomial_degree_eta, nEl_eta)
            knot_vector_zeta = Node_vector(polynomial_degree_zeta, nEl_zeta)
        knot_vector_objs = [self.knot_vector_r, knot_vector_eta, knot_vector_zeta]
        degrees = (
            self.polynomial_degree_r,
            polynomial_degree_eta,
            polynomial_degree_zeta,
        )

        # Build Bezier patches from B-spline control points
        from cardillo.discretization.B_spline import decompose_B_spline_volume

        Qw = decompose_B_spline_volume(knot_vector_objs, Pw)

        nbezier_xi, nbezier_eta, nbezier_zeta, p1, q1, r1, dim = Qw.shape

        # build vtk mesh
        n_patches = nbezier_xi * nbezier_eta * nbezier_zeta
        patch_size = p1 * q1 * r1
        points = np.zeros((n_patches * patch_size, dim))
        cells = []
        HigherOrderDegrees = []
        RationalWeights = []
        vtk_cell_type = "VTK_BEZIER_HEXAHEDRON"
        from PyPanto.miscellaneous.indexing import flat3D, rearange_vtk3D

        for i in range(nbezier_xi):
            for j in range(nbezier_eta):
                for k in range(nbezier_zeta):
                    idx = flat3D(i, j, k, (nbezier_xi, nbezier_eta))
                    point_range = np.arange(idx * patch_size, (idx + 1) * patch_size)
                    points[point_range] = rearange_vtk3D(Qw[i, j, k])

                    cells.append((vtk_cell_type, point_range[None]))
                    HigherOrderDegrees.append(np.array(degrees, dtype=float)[None])
                    weight = np.sqrt(2) / 2
                    # tmp = np.array([np.sqrt(2) / 2, 1.0])
                    # RationalWeights.append(np.tile(tmp, 8)[None])
                    weights_vertices = weight * np.ones(8)
                    weights_edges = np.ones(4 * nn_xi)
                    weights_faces = np.ones(2)
                    weights_volume = np.ones(nn_xi - 2)
                    weights = np.concatenate(
                        (weights_edges, weights_vertices, weights_faces, weights_volume)
                    )
                    # weights = np.array([weight, weight, weight, weight,
                    #                     1.0,    1.0,    1.0,    1.0,
                    #                     0.0,    0.0,    0.0,    0.0 ])
                    weights = np.ones_like(point_range)
                    RationalWeights.append(weights[None])

        # RationalWeights = np.ones(len(points))
        RationalWeights = 2 * (np.random.rand(len(points)) + 1)

        # write vtk mesh using meshio
        meshio.write_points_cells(
            # filename.parent / (filename.stem + '.vtu'),
            filename,
            points,
            cells,
            point_data={
                "RationalWeights": RationalWeights,
            },
            cell_data={"HigherOrderDegrees": HigherOrderDegrees},
            binary=binary,
        )

    def post_processing_vtk_volume(self, t, q, filename, circular=True, binary=False):
        # This is mandatory, otherwise we cannot construct the 3D continuum without L2 projection!
        assert (
            self.polynomial_degree_r == self.polynomial_degree_psi
        ), "Not implemented for mixed polynomial degrees"

        # rearrange generalized coordinates from solver ordering to Piegl's Pw 3D array
        nn_xi = self.nelement + self.polynomial_degree_r
        nEl_eta = 1
        nEl_zeta = 1
        if circular:
            polynomial_degree_eta = 2
            polynomial_degree_zeta = 2
        else:
            polynomial_degree_eta = 1
            polynomial_degree_zeta = 1
        nn_eta = nEl_eta + polynomial_degree_eta
        nn_zeta = nEl_zeta + polynomial_degree_zeta

        # TODO: We do the hard coded case for rectangular cross section here, but this has to be extended to the circular cross section case too!
        if circular:
            r = 0.2
            a = b = r
        else:
            a = 0.2
            b = 0.1
        as_ = np.linspace(-a / 2, a / 2, num=nn_eta, endpoint=True)
        bs_ = np.linspace(-b / 2, b / 2, num=nn_eta, endpoint=True)

        Pw = np.zeros((nn_xi, nn_eta, nn_zeta, 3))
        for i in range(self.nnode_r):
            qr = q[self.nodalDOF_r[i]]
            q_di = q[self.nodalDOF_psi[i]]
            A_IK = q_di.reshape(3, 3, order="F")  # TODO: Check this!

            for j, aj in enumerate(as_):
                for k, bk in enumerate(bs_):
                    Pw[i, j, k] = qr + A_IK @ np.array([0, aj, bk])

        if self.basis == "B-spline":
            knot_vector_eta = KnotVector(polynomial_degree_eta, nEl_eta)
            knot_vector_zeta = KnotVector(polynomial_degree_zeta, nEl_zeta)
        elif self.basis == "lagrange":
            knot_vector_eta = Node_vector(polynomial_degree_eta, nEl_eta)
            knot_vector_zeta = Node_vector(polynomial_degree_zeta, nEl_zeta)
        knot_vector_objs = [self.knot_vector_r, knot_vector_eta, knot_vector_zeta]
        degrees = (
            self.polynomial_degree_r,
            polynomial_degree_eta,
            polynomial_degree_zeta,
        )

        # Build Bezier patches from B-spline control points
        from cardillo.discretization.B_spline import decompose_B_spline_volume

        Qw = decompose_B_spline_volume(knot_vector_objs, Pw)

        nbezier_xi, nbezier_eta, nbezier_zeta, p1, q1, r1, dim = Qw.shape

        # build vtk mesh
        n_patches = nbezier_xi * nbezier_eta * nbezier_zeta
        patch_size = p1 * q1 * r1
        points = np.zeros((n_patches * patch_size, dim))
        cells = []
        HigherOrderDegrees = []
        RationalWeights = []
        vtk_cell_type = "VTK_BEZIER_HEXAHEDRON"
        from PyPanto.miscellaneous.indexing import flat3D, rearange_vtk3D

        for i in range(nbezier_xi):
            for j in range(nbezier_eta):
                for k in range(nbezier_zeta):
                    idx = flat3D(i, j, k, (nbezier_xi, nbezier_eta))
                    point_range = np.arange(idx * patch_size, (idx + 1) * patch_size)
                    points[point_range] = rearange_vtk3D(Qw[i, j, k])

                    cells.append((vtk_cell_type, point_range[None]))
                    HigherOrderDegrees.append(np.array(degrees, dtype=float)[None])
                    weight = np.sqrt(2) / 2
                    # tmp = np.array([np.sqrt(2) / 2, 1.0])
                    # RationalWeights.append(np.tile(tmp, 8)[None])
                    weights_vertices = weight * np.ones(8)
                    weights_edges = np.ones(4 * nn_xi)
                    weights_faces = np.ones(2)
                    weights_volume = np.ones(nn_xi - 2)
                    weights = np.concatenate(
                        (weights_edges, weights_vertices, weights_faces, weights_volume)
                    )
                    # weights = np.array([weight, weight, weight, weight,
                    #                     1.0,    1.0,    1.0,    1.0,
                    #                     0.0,    0.0,    0.0,    0.0 ])
                    weights = np.ones_like(point_range)
                    RationalWeights.append(weights[None])

        # RationalWeights = np.ones(len(points))
        RationalWeights = 2 * (np.random.rand(len(points)) + 1)

        # write vtk mesh using meshio
        meshio.write_points_cells(
            # filename.parent / (filename.stem + '.vtu'),
            filename,
            points,
            cells,
            point_data={
                "RationalWeights": RationalWeights,
            },
            cell_data={"HigherOrderDegrees": HigherOrderDegrees},
            binary=binary,
        )

    def post_processing(self, t, q, filename, binary=True):
        # write paraview PVD file collecting time and all vtk files, see https://www.paraview.org/Wiki/ParaView/Data_formats#PVD_File_Format
        from xml.dom import minidom

        root = minidom.Document()

        vkt_file = root.createElement("VTKFile")
        vkt_file.setAttribute("type", "Collection")
        root.appendChild(vkt_file)

        collection = root.createElement("Collection")
        vkt_file.appendChild(collection)

        for i, (ti, qi) in enumerate(zip(t, q)):
            filei = filename + f"{i}.vtu"

            # write time step and file name in pvd file
            dataset = root.createElement("DataSet")
            dataset.setAttribute("timestep", f"{ti:0.6f}")
            dataset.setAttribute("file", filei)
            collection.appendChild(dataset)

            self.post_processing_single_configuration(ti, qi, filei, binary=binary)

        # write pvd file
        xml_str = root.toprettyxml(indent="\t")
        with open(filename + ".pvd", "w") as f:
            f.write(xml_str)

    def post_processing_single_configuration(self, t, q, filename, binary=True):
        # centerline and connectivity
        cells_r, points_r, HigherOrderDegrees_r = self.mesh_r.vtk_mesh(q[: self.nq_r])

        # if the centerline and the directors are interpolated with the same
        # polynomial degree we can use the values on the nodes and decompose the B-spline
        # into multiple Bezier patches, otherwise the directors have to be interpolated
        # onto the nodes of the centerline by a so-called L2-projection, see below
        same_shape_functions = False
        if self.polynomial_degree_r == self.polynomial_degree_psi:
            same_shape_functions = True

        if same_shape_functions:
            _, points_di, _ = self.mesh_psi.vtk_mesh(q[self.nq_r :])

            # fill dictionary storing point data with directors
            point_data = {
                "d1": points_di[:, 0:3],
                "d2": points_di[:, 3:6],
                "d3": points_di[:, 6:9],
            }

        else:
            point_data = {}

        # export existing values on quadrature points using L2 projection
        J0_vtk = self.mesh_r.field_to_vtk(
            self.J.reshape(self.nelement, self.nquadrature, 1)
        )
        point_data.update({"J0": J0_vtk})

        Gamma0_vtk = self.mesh_r.field_to_vtk(self.Gamma0)
        point_data.update({"Gamma0": Gamma0_vtk})

        Kappa0_vtk = self.mesh_r.field_to_vtk(self.Kappa0)
        point_data.update({"Kappa0": Kappa0_vtk})

        # evaluate fields at quadrature points that have to be projected onto the centerline mesh:
        # - strain measures Gamma & Kappa
        # - directors d1, d2, d3
        Gamma = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
        Kappa = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
        if not same_shape_functions:
            d1s = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
            d2s = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
            d3s = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
        for el in range(self.nelement):
            qe = q[self.elDOF[el]]

            # extract generalized coordinates for beam centerline and directors
            # in the current and reference configuration
            qe_r = qe[self.rDOF]
            qe_d1 = qe[self.psiDOF]
            qe_d2 = qe[self.d2DOF]
            qe_d3 = qe[self.d3DOF]

            for i in range(self.nquadrature):
                # build matrix of shape function derivatives
                NN_di_i = self.stack3psi(self.N_psi[el, i])
                NN_r_xii = self.stack3r(self.N_r_xi[el, i])
                NN_di_xii = self.stack3psi(self.N_psi_xi[el, i])

                # extract reference state variables
                J0i = self.J[el, i]
                Gamma0_i = self.Gamma0[el, i]
                Kappa0_i = self.Kappa0[el, i]

                # Interpolate necessary quantities. The derivatives are evaluated w.r.t.
                # the parameter space \xi and thus need to be transformed later
                r_xi = NN_r_xii @ qe_r

                d1 = NN_di_i @ qe_d1
                d1_xi = NN_di_xii @ qe_d1

                d2 = NN_di_i @ qe_d2
                d2_xi = NN_di_xii @ qe_d2

                d3 = NN_di_i @ qe_d3
                d3_xi = NN_di_xii @ qe_d3

                # compute derivatives w.r.t. the arc lenght parameter s
                r_s = r_xi / J0i

                d1_s = d1_xi / J0i
                d2_s = d2_xi / J0i
                d3_s = d3_xi / J0i

                # build rotation matrices
                if not same_shape_functions:
                    d1s[el, i] = d1
                    d2s[el, i] = d2
                    d3s[el, i] = d3
                R = np.vstack((d1, d2, d3)).T

                # axial and shear strains
                Gamma[el, i] = R.T @ r_s

                # torsional and flexural strains
                Kappa[el, i] = np.array(
                    [
                        0.5 * (d3 @ d2_s - d2 @ d3_s),
                        0.5 * (d1 @ d3_s - d3 @ d1_s),
                        0.5 * (d2 @ d1_s - d1 @ d2_s),
                    ]
                )

        # L2 projection of strain measures
        Gamma_vtk = self.mesh_r.field_to_vtk(Gamma)
        point_data.update({"Gamma": Gamma_vtk})

        Kappa_vtk = self.mesh_r.field_to_vtk(Kappa)
        point_data.update({"Kappa": Kappa_vtk})

        # L2 projection of directors
        if not same_shape_functions:
            d1_vtk = self.mesh_r.field_to_vtk(d1s)
            point_data.update({"d1": d1_vtk})
            d2_vtk = self.mesh_r.field_to_vtk(d2s)
            point_data.update({"d2": d2_vtk})
            d3_vtk = self.mesh_r.field_to_vtk(d3s)
            point_data.update({"d3": d3_vtk})

        # fields depending on strain measures and other previously computed quantities
        point_data_fields = {
            "W": lambda Gamma, Gamma0, Kappa, Kappa0: np.array(
                [self.material_model.potential(Gamma, Gamma0, Kappa, Kappa0)]
            ),
            "n_i": lambda Gamma, Gamma0, Kappa, Kappa0: self.material_model.n_i(
                Gamma, Gamma0, Kappa, Kappa0
            ),
            "m_i": lambda Gamma, Gamma0, Kappa, Kappa0: self.material_model.m_i(
                Gamma, Gamma0, Kappa, Kappa0
            ),
        }

        for name, fun in point_data_fields.items():
            tmp = fun(Gamma_vtk[0], Gamma0_vtk[0], Kappa_vtk[0], Kappa0_vtk[0]).reshape(
                -1
            )
            field = np.zeros((len(Gamma_vtk), len(tmp)))
            for i, (Gamma_i, Gamma0_i, Kappa_i, Kappa0_i) in enumerate(
                zip(Gamma_vtk, Gamma0_vtk, Kappa_vtk, Kappa0_vtk)
            ):
                field[i] = fun(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i).reshape(-1)
            point_data.update({name: field})

        # write vtk mesh using meshio
        meshio.write_points_cells(
            os.path.splitext(os.path.basename(filename))[0] + ".vtu",
            points_r,  # only export centerline as geometry here!
            cells_r,
            point_data=point_data,
            cell_data={"HigherOrderDegrees": HigherOrderDegrees_r},
            binary=binary,
        )

    def post_processing_subsystem(self, t, q, u, binary=True):
        # centerline and connectivity
        cells_r, points_r, HigherOrderDegrees_r = self.mesh_r.vtk_mesh(q[: self.nq_r])

        # if the centerline and the directors are interpolated with the same
        # polynomial degree we can use the values on the nodes and decompose the B-spline
        # into multiple Bezier patches, otherwise the directors have to be interpolated
        # onto the nodes of the centerline by a so-called L2-projection, see below
        same_shape_functions = False
        if self.polynomial_degree_r == self.polynomial_degree_psi:
            same_shape_functions = True

        if same_shape_functions:
            _, points_di, _ = self.mesh_psi.vtk_mesh(q[self.nq_r :])

            # fill dictionary storing point data with directors
            point_data = {
                "u_r": points_r - points_r[0],
                "d1": points_di[:, 0:3],
                "d2": points_di[:, 3:6],
                "d3": points_di[:, 6:9],
            }

        else:
            point_data = {}

        # export existing values on quadrature points using L2 projection
        J0_vtk = self.mesh_r.field_to_vtk(
            self.J.reshape(self.nelement, self.nquadrature, 1)
        )
        point_data.update({"J0": J0_vtk})

        Gamma0_vtk = self.mesh_r.field_to_vtk(self.Gamma0)
        point_data.update({"Gamma0": Gamma0_vtk})

        Kappa0_vtk = self.mesh_r.field_to_vtk(self.Kappa0)
        point_data.update({"Kappa0": Kappa0_vtk})

        # evaluate fields at quadrature points that have to be projected onto the centerline mesh:
        # - strain measures Gamma & Kappa
        # - directors d1, d2, d3
        Gamma = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
        Kappa = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
        if not same_shape_functions:
            d1s = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
            d2s = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
            d3s = np.zeros((self.mesh_r.nelement, self.mesh_r.n_quadrature_points, 3))
        for el in range(self.nelement):
            qe = q[self.elDOF[el]]

            # extract generalized coordinates for beam centerline and directors
            # in the current and reference configuration
            qe_r = qe[self.rDOF]
            qe_d1 = qe[self.psiDOF]
            qe_d2 = qe[self.d2DOF]
            qe_d3 = qe[self.d3DOF]

            for i in range(self.nquadrature):
                # build matrix of shape function derivatives
                NN_di_i = self.stack3psi(self.N_psi[el, i])
                NN_r_xii = self.stack3r(self.N_r_xi[el, i])
                NN_di_xii = self.stack3psi(self.N_psi_xi[el, i])

                # extract reference state variables
                J0i = self.J[el, i]
                Gamma0_i = self.Gamma0[el, i]
                Kappa0_i = self.Kappa0[el, i]

                # Interpolate necessary quantities. The derivatives are evaluated w.r.t.
                # the parameter space \xi and thus need to be transformed later
                r_xi = NN_r_xii @ qe_r

                d1 = NN_di_i @ qe_d1
                d1_xi = NN_di_xii @ qe_d1

                d2 = NN_di_i @ qe_d2
                d2_xi = NN_di_xii @ qe_d2

                d3 = NN_di_i @ qe_d3
                d3_xi = NN_di_xii @ qe_d3

                # compute derivatives w.r.t. the arc lenght parameter s
                r_s = r_xi / J0i

                d1_s = d1_xi / J0i
                d2_s = d2_xi / J0i
                d3_s = d3_xi / J0i

                # build rotation matrices
                if not same_shape_functions:
                    d1s[el, i] = d1
                    d2s[el, i] = d2
                    d3s[el, i] = d3
                R = np.vstack((d1, d2, d3)).T

                # axial and shear strains
                Gamma[el, i] = R.T @ r_s

                # torsional and flexural strains
                Kappa[el, i] = np.array(
                    [
                        0.5 * (d3 @ d2_s - d2 @ d3_s),
                        0.5 * (d1 @ d3_s - d3 @ d1_s),
                        0.5 * (d2 @ d1_s - d1 @ d2_s),
                    ]
                )

        # L2 projection of strain measures
        Gamma_vtk = self.mesh_r.field_to_vtk(Gamma)
        point_data.update({"Gamma": Gamma_vtk})

        Kappa_vtk = self.mesh_r.field_to_vtk(Kappa)
        point_data.update({"Kappa": Kappa_vtk})

        # L2 projection of directors
        if not same_shape_functions:
            d1_vtk = self.mesh_r.field_to_vtk(d1s)
            point_data.update({"d1": d1_vtk})
            d2_vtk = self.mesh_r.field_to_vtk(d2s)
            point_data.update({"d2": d2_vtk})
            d3_vtk = self.mesh_r.field_to_vtk(d3s)
            point_data.update({"d3": d3_vtk})

        # fields depending on strain measures and other previously computed quantities
        point_data_fields = {
            "W": lambda Gamma, Gamma0, Kappa, Kappa0: np.array(
                [self.material_model.potential(Gamma, Gamma0, Kappa, Kappa0)]
            ),
            "n_i": lambda Gamma, Gamma0, Kappa, Kappa0: self.material_model.n_i(
                Gamma, Gamma0, Kappa, Kappa0
            ),
            "m_i": lambda Gamma, Gamma0, Kappa, Kappa0: self.material_model.m_i(
                Gamma, Gamma0, Kappa, Kappa0
            ),
        }

        for name, fun in point_data_fields.items():
            tmp = fun(Gamma_vtk[0], Gamma0_vtk[0], Kappa_vtk[0], Kappa0_vtk[0]).reshape(
                -1
            )
            field = np.zeros((len(Gamma_vtk), len(tmp)))
            for i, (Gamma_i, Gamma0_i, Kappa_i, Kappa0_i) in enumerate(
                zip(Gamma_vtk, Gamma0_vtk, Kappa_vtk, Kappa0_vtk)
            ):
                field[i] = fun(Gamma_i, Gamma0_i, Kappa_i, Kappa0_i).reshape(-1)
            point_data.update({name: field})

        return points_r, point_data, cells_r, HigherOrderDegrees_r