import numpy as np
from scipy.sparse.linalg import spsolve
from scipy.sparse import csr_matrix, bmat, eye
from tqdm import tqdm

from cardillo.math import approx_fprime
from cardillo.solver import Solution
from cardillo.math import prox_Rn0, prox_sphere


class GenAlphaFirstOrderVelocityGGLNormalContacts:
    """Generalized alpha solver for first order ODE's with GLL stabilization.
    
    To-Do:
    -----
    * Add GGL stabilization for constraints on position level in kinematic 
      differential equation in order to solve an index 2 DAE system 
    * Think about preconditioning according to Arnold2008 and Bottasso2008?

    References
    ----------
    Arnold2008: https://doi.org/10.1007/s11044-007-9084-0 \\
    Bottasso2008 https://doi.org/10.1007/s11044-007-9051-9
    """

    def __init__(
        self,
        model,
        t1,
        dt,
        rho_inf=1,
        tol=1e-8,
        max_iter=40,
        error_function=lambda x: np.max(np.abs(x)),
        numerical_jacobian=True,
        DAE_index=2,
    ):

        self.model = model
        assert DAE_index >= 1 and DAE_index <= 2, "DAE_index hast to be in [1, 2]!"
        self.DAE_index = DAE_index

        #######################################################################
        # integration time
        #######################################################################
        self.t0 = t0 = model.t0
        self.t1 = (
            t1 if t1 > t0 else ValueError("t1 must be larger than initial time t0.")
        )
        self.dt = dt

        #######################################################################
        # gen alpha parameter
        #######################################################################
        self.rho_inf = rho_inf
        # self.alpha_m = 0.5 * (3. - rho_inf) / (1. + rho_inf) # Jansen2000 (23)
        # self.alpha_f = 1. / (1. + rho_inf) # Jansen2000 (23)
        # self.gamma = 0.5 + self.alpha_m - self.alpha_f # Jansen2000 (17)

        self.alpha_m = (2.0 * rho_inf - 1.0) / (rho_inf + 1.0)  # Arnold2007 (24)
        self.alpha_f = rho_inf / (rho_inf + 1.0)  # Arnold2007 (24)
        self.gamma = 0.5 + self.alpha_f - self.alpha_m  # Arnold2007 (24)
        self.beta = 0.25 * ((self.gamma + 0.5) ** 2)  # TODO: Reference!

        #######################################################################
        # newton settings
        #######################################################################
        self.tol = tol
        self.max_iter = max_iter
        self.error_function = error_function

        #######################################################################
        # dimensions
        #######################################################################
        self.nq = model.nq
        self.nu = model.nu
        self.nla_g = model.nla_g
        self.nla_gamma = model.nla_gamma
        self.nla_N = model.nla_N
        self.ny = self.nq + self.nu  # dimension of the state space
        self.nx = self.ny + 3 * self.nla_g + self.nla_gamma + 3 * self.nla_N

        #######################################################################
        # consistent initial conditions
        #######################################################################
        self.tk = model.t0
        self.qk = model.q0
        self.uk = model.u0
        self.kappa_gk = np.zeros_like(model.la_g0)
        self.la_gk = model.la_g0
        self.La_gk = np.zeros_like(model.la_g0)
        self.la_gammak = model.la_gamma0
        self.kappa_Nk = np.zeros_like(model.la_N0)
        self.la_Nk = model.la_N0
        self.La_Nk = np.zeros_like(model.la_N0)

        # TODO: Move these into xk in a more involved version
        self.la_gbark = self.la_Nk.copy()
        self.la_Nbark = self.la_Nk.copy()

        self.q_dotk = self.model.q_dot(self.tk, self.qk, self.uk)
        M0 = self.model.M(self.tk, self.qk, scipy_matrix=csr_matrix)
        h0 = self.model.h(self.tk, self.qk, self.uk)
        W_g0 = self.model.W_g(self.tk, self.qk, scipy_matrix=csr_matrix)
        W_gamma0 = self.model.W_gamma(self.tk, self.qk, scipy_matrix=csr_matrix)
        W_N0 = self.model.W_N(self.tk, self.qk, scipy_matrix=csr_matrix)
        self.u_dotk = spsolve(
            M0, h0 + W_g0 @ self.la_gk + W_gamma0 @ self.la_gammak + W_N0 @ self.la_Nk
        )

        # check if initial conditions satisfy constraints on position, velocity
        # and acceleration level
        g0 = model.g(self.tk, self.qk)
        g_dot0 = model.g_dot(self.tk, self.qk, self.uk)
        g_ddot0 = model.g_ddot(self.tk, self.qk, self.uk, self.u_dotk)
        gamma0 = model.gamma(self.tk, self.qk, self.uk)
        gamma_dot0 = model.gamma_dot(self.tk, self.qk, self.uk, self.u_dotk)

        assert np.allclose(
            g0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g0!"
        assert np.allclose(
            g_dot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_dot0!"
        assert np.allclose(
            g_ddot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_ddot0!"
        assert np.allclose(
            gamma0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma0!"
        assert np.allclose(
            gamma_dot0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma_dot0!"

        # check if unilateral constraints are satisfied
        g_N0 = model.g_N(self.tk, self.qk)
        assert np.all(g_N0 >= 0), "Initial conditions do not fulfill g_N0 >= 0!"

        # TODO: Check g_dotN0 and g_ddotN0 as well!

        #######################################################################
        # starting values for generalized state vector, its derivatives and
        # auxiliary velocities
        #######################################################################
        self.yk = np.concatenate((self.qk, self.uk))
        self.y_dotk = np.concatenate((self.q_dotk, self.u_dotk))
        self.vk = self.y_dotk.copy()  # TODO: Is there a better choice?
        self.xk = self.pack(
            self.q_dotk,
            self.u_dotk,
            self.kappa_gk,
            self.La_gk,
            self.la_gk,
            self.la_gammak,  # TODO: Did we missed La_gammak, i.e, impulsive contact forces for bil. constr. on velocity level?
            self.kappa_Nk,
            self.La_Nk,
            self.la_Nk,
        )

        if numerical_jacobian:
            self.__R_gen = self.__R_gen_num
        else:
            self.__R_gen = self.__R_gen_analytic

        # TODO: Remove this
        self.k = 0

    def update(self, y_dotk1, store=False):
        """Update dependent variables modifed version of Arnold2008 (3) and (5)."""
        nq = self.nq
        nu = self.nu

        # constants
        dt = self.dt
        gamma = self.gamma
        alpha_f = self.alpha_f
        alpha_m = self.alpha_m

        # auxiliary variables, see Arnold2008 (3)
        vk1 = (
            alpha_f * self.y_dotk + (1.0 - alpha_f) * y_dotk1 - alpha_m * self.vk
        ) / (1.0 - alpha_m)

        # approximation of the single integral, see Arnold2008 (5)
        yk1 = self.yk + dt * ((1.0 - gamma) * self.vk + gamma * vk1)

        if store:
            self.vk = vk1.copy()
            self.yk = yk1.copy()
            self.y_dotk = y_dotk1.copy()
            self.uk = vk1[nq : nq + nu]  # TODO: This is important!

        # extract generaliezd coordinates and velocities
        qk1 = yk1[:nq]
        uk1 = yk1[nq : nq + nu]
        q_dotk1 = y_dotk1[:nq]
        u_dotk1 = y_dotk1[nq : nq + nu]
        return qk1, uk1, q_dotk1, u_dotk1

    def pack(self, q_dot, u_dot, kappa_g, La_g, la_g, la_gamma, kappa_N, La_N, la_N):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma
        nla_N = self.nla_N

        x = np.zeros(self.nx)

        x[:nq] = q_dot
        x[nq : nq + nu] = u_dot
        x[nq + nu : nq + nu + nla_g] = kappa_g
        x[nq + nu + nla_g : nq + nu + 2 * nla_g] = La_g
        x[nq + nu + 2 * nla_g : nq + nu + 3 * nla_g] = la_g
        x[nq + nu + 3 * nla_g : nq + nu + 3 * nla_g + nla_gamma] = la_gamma
        x[
            nq + nu + 3 * nla_g + nla_gamma : nq + nu + 3 * nla_g + nla_gamma + nla_N
        ] = kappa_N
        x[
            nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + nla_N : nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + 2 * nla_N
        ] = La_N
        x[
            nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + 2 * nla_N : nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + 3 * nla_N
        ] = la_N

        return x

    def unpack(self, x):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma
        nla_N = self.nla_N

        q_dot = x[:nq].copy()
        u_dot = x[nq : nq + nu].copy()
        kappa_g = x[nq + nu : nq + nu + nla_g].copy()
        La_g = x[nq + nu + nla_g : nq + nu + 2 * nla_g].copy()
        la_g = x[nq + nu + 2 * nla_g : nq + nu + 3 * nla_g].copy()
        la_gamma = x[nq + nu + 3 * nla_g : nq + nu + 3 * nla_g + nla_gamma].copy()
        kappa_N = x[
            nq + nu + 3 * nla_g + nla_gamma : nq + nu + 3 * nla_g + nla_gamma + nla_N
        ].copy()
        La_N = x[
            nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + nla_N : nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + 2 * nla_N
        ].copy()
        la_N = x[
            nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + 2 * nla_N : nq
            + nu
            + 3 * nla_g
            + nla_gamma
            + 3 * nla_N
        ].copy()

        return q_dot, u_dot, kappa_g, La_g, la_g, la_gamma, kappa_N, La_N, la_N

    def __R_gen_num(self, tk1, xk1):
        yield self.__R(tk1, xk1)
        yield csr_matrix(self.__R_x_num(tk1, xk1))

    # def __R_gen_analytic(self, tk1, xk1, update_index_set=True):
    def R(self, tk1, xk1, update_index_set=False):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma
        nla_N = self.nla_N

        # extract generalized coordinates and velocities of the previous time step
        # qk = self.xk[:nq]
        # uk = self.xk[nq:nq+nu]
        uk = self.uk

        # extract auxiliary velocities and Lagrange multipliers
        y_dotk1 = xk1[: self.ny]
        # la_gk1 = xk1[self.ny:self.ny+nla_g]
        # la_gammak1 = xk1[self.ny+nla_g:nq+nu+nla_g+nla_gamma]
        # mu_gk1 = xk1[nq+nu+nla_g+nla_gamma:nq+nu+2*nla_g+nla_gamma]
        # P_Nk1 = xk1[nq+nu+2*nla_g+nla_gamma:nq+nu+2*nla_g+nla_gamma+nla_N]
        # mu_Nk1 = xk1[nq+nu+2*nla_g+nla_gamma+nla_N:nq+nu+2*nla_g+nla_gamma+2*nla_N]
        # nu_Nk1 = xk1[nq+nu+2*nla_g+nla_gamma+2*nla_N:nq+nu+2*nla_g+nla_gamma+3*nla_N]

        (
            q_dotk1,
            u_dotk1,
            kappa_gk1,
            La_gk1,
            la_gk1,
            la_gammak1,
            kappa_Nk1,
            La_Nk1,
            la_Nk1,
        ) = self.unpack(xk1)

        ############################
        # update dependent variables
        ############################
        qk1, uk1, q_dotk1, u_dotk1 = self.update(y_dotk1, store=False)

        # gen alpha udate for Lagrange multipliers
        la_Nbark1 = (
            self.alpha_f * self.la_Nk
            + (1 - self.alpha_f) * la_Nk1
            - self.alpha_m * self.la_Nbark
        ) / (1 - self.alpha_m)
        P_Nk1 = La_Nk1 + self.dt * (
            (1 - self.gamma) * self.la_Nbark + self.gamma * la_Nbark1
        )

        # TODO: Can we ommmit the integrated la_Nbar using second gen alpha scheme?
        kappa_hatNk1 = kappa_Nk1 + self.dt**2 * (
            (0.5 - self.beta) * self.la_Nbark + self.beta * la_Nbark1
        )

        #####################################
        # evaluate repeatedly used quantities
        #####################################
        Mk1 = self.model.M(tk1, qk1)

        W_gk1 = self.model.W_g(tk1, qk1)
        W_gammak1 = self.model.W_gamma(tk1, qk1)
        W_Nk1 = self.model.W_N(tk1, qk1, scipy_matrix=csr_matrix)

        g_Nk1 = self.model.g_N(tk1, qk1)
        xi_Nk1 = self.model.xi_N(tk1, qk1, uk, uk1)
        g_N_ddotk1 = self.model.g_N_ddot(tk1, qk1, uk1, u_dotk1)

        ###################
        # evaluate residual
        ###################
        R = np.zeros(self.nx)

        # kinematic differential equation
        R[:nq] = (
            q_dotk1
            - self.model.q_dot(tk1, qk1, uk1)
            - W_gk1 @ kappa_gk1
            - W_Nk1 @ kappa_Nk1
        )

        # equations of motion
        R[nq : nq + nu] = (
            Mk1 @ u_dotk1
            - self.model.h(tk1, qk1, uk1)
            - W_gk1 @ la_gk1
            - W_gammak1 @ la_gammak1
            - W_Nk1 @ la_Nk1
        )
        #   - W_Nk1 @ (la_Nk1 + La_Nk1)

        R[nq + nu : nq + nu + nla_g] = self.model.g(tk1, qk1)
        R[nq + nu + nla_g : nq + nu + 2 * nla_g] = self.model.g_dot(tk1, qk1, uk1)
        R[nq + nu + 2 * nla_g : nq + nu + 3 * nla_g] = self.model.g_ddot(
            tk1, qk1, uk1, u_dotk1
        )
        R[nq + nu + 3 * nla_g : nq + nu + 3 * nla_g + nla_gamma] = self.model.gamma(
            tk1, qk1, uk1
        )

        # TODO: Move in constructor!
        self.nR_smooth = nq + nu + 3 * nla_g + nla_gamma

        #####################
        # unilateral contacts
        #####################

        # update index sets
        if update_index_set:
            # eqn. (130):
            self.Ai1 = self.model.prox_r_N * g_Nk1 - kappa_hatNk1 <= 0
            # eqn. (133):
            self.Bi1 = self.Ai1 * ((self.model.prox_r_N * xi_Nk1 - P_Nk1) <= 0)
            # eqn. (136):
            self.Ci1 = self.Bi1 * ((self.model.prox_r_N * g_N_ddotk1 - la_Nk1) <= 0)

        I_N = self.Ai1
        I_N_ind = np.where(I_N)[0]
        _I_N_ind = np.where(~I_N)[0]
        R[self.nR_smooth + I_N_ind] = g_Nk1[I_N]
        R[self.nR_smooth + _I_N_ind] = kappa_hatNk1[~I_N]

        A_N_ = self.Bi1
        A_N = I_N * A_N_
        A_N_ind = np.where(A_N)[0]
        _A_N_ind = np.where(~A_N)[0]
        R[self.nR_smooth + nla_N + A_N_ind] = xi_Nk1[A_N]
        R[self.nR_smooth + nla_N + _A_N_ind] = P_Nk1[~A_N]

        B_N_ = self.Ci1
        B_N = A_N * B_N_
        B_N_ind = np.where(B_N)[0]
        _B_N_ind = np.where(~B_N)[0]
        R[self.nR_smooth + 2 * nla_N + B_N_ind] = g_N_ddotk1[B_N]
        R[self.nR_smooth + 2 * nla_N + _B_N_ind] = la_Nk1[~B_N]

        # if self.k == 6:
        #     print(f"k: {self.k}")

        ##########
        # old code
        ##########

        # # unilateral contacts
        # B_N_ind = np.where(B_N)[0] + nq+nu+2*nla_g+nla_gamma
        # _B_N_ind = np.where(~B_N)[0] + nq+nu+2*nla_g+nla_gamma
        # # R[I_N_ind] = P_Nk1[I_N] - prox_Rn0(P_Nk1[I_N] - self.model.prox_r_N[I_N] * xi_Nk1[I_N])
        # R[B_N_ind] = xi_Nk1[B_N]
        # R[_B_N_ind] = P_Nk1[~B_N]

        # R[nq+nu+2*nla_g+nla_gamma+nla_N:nq+nu+2*nla_g+nla_gamma+2*nla_N] = mu_Nk1 - prox_Rn0(mu_Nk1 - self.model.prox_r_N * g_Nk1)

        # A_N_ind = np.where(A_N)[0] + nq+nu+2*nla_g+nla_gamma+2*nla_N
        # _A_N_ind = np.where(~A_N)[0] + nq+nu+2*nla_g+nla_gamma+2*nla_N
        # # R[A_N_ind] = mu_Nk1[A_N] - prox_Rn0(mu_Nk1[A_N] - self.model.prox_r_N[A_N] * g_Nk1[A_N])
        # R[A_N_ind] = g_N_dotk1[A_N]
        # R[_A_N_ind] = nu_Nk1[~A_N]

        # yield R
        # raise RuntimeError("...")

        return R

    def __R(self, tk1, xk1, update_index_set=True):
        return next(self.__R_gen_analytic(tk1, xk1, update_index_set))

    def __R_x_num(self, tk1, xk1):
        return approx_fprime(
            xk1, lambda x: self.__R(tk1, x, update_index_set=True), method="2-point"
        )

    def step(self, tk1, xk1):
        # initial residual and error
        # R_gen = self.__R_gen(tk1, xk1)
        # R = next(R_gen)
        R = self.R(tk1, xk1, update_index_set=True)
        error = self.error_function(R)
        converged = error < self.tol
        j = 0
        if not converged:
            while j < self.max_iter:
                # jacobian
                # R_x = next(R_gen)
                R_x_dense = approx_fprime(
                    xk1,
                    lambda x: self.__R(tk1, x, update_index_set=False),
                    method="2-point",
                )
                R_x = csr_matrix(R_x_dense)

                # TODO: Nonsmooth jacobian is identity for contact less case which is correct. By why is it zero for the contact case?
                if self.k == 6:
                    print(f"k: {self.k}")
                    # R_x_dense = R_x.toarray()
                    print(f"R_x_NS:\n{R_x_dense[self.nR_smooth:, self.nR_smooth:]}")
                    print(
                        f"R_x_dense[nR_smooth:, :nq]:\n{R_x_dense[self.nR_smooth:, :self.nq]}"
                    )
                    print(
                        f"R_x_dense[nR_smooth:, nq:nq+nu]:\n{R_x_dense[self.nR_smooth:, self.nq:self.nq+self.nu]}"
                    )
                    print(f"")

                # Newton update
                j += 1
                dx = spsolve(R_x, R, use_umfpack=True)
                xk1 -= dx
                # R_gen = self.__R_gen(tk1, xk1)
                # R = next(R_gen)
                R = self.R(tk1, xk1, update_index_set=True)

                error = self.error_function(R)
                converged = error < self.tol
                if converged:
                    break

        return converged, j, error, xk1

    def solve(self):
        # lists storing output variables
        # TODO: Update output variables
        t = [self.tk]
        q = [self.qk]
        u = [self.uk]
        q_dot = [self.q_dotk]
        u_dot = [self.u_dotk]
        kappa_g = [self.kappa_gk]
        La_g = [self.La_gk]
        la_g = [self.la_gk]
        la_gamma = [self.la_gammak]
        kappa_N = [self.kappa_Nk]
        La_N = [self.La_Nk]
        la_N = [self.la_Nk]

        pbar = tqdm(np.arange(self.t0, self.t1, self.dt))
        for k, _ in enumerate(pbar):
            self.k = k  # TODO: remove this!

            # perform a sovler step
            tk1 = self.tk + self.dt
            xk1 = self.xk.copy()  # This copy is mandatory since we modify xk1
            # in the step function
            converged, n_iter, error, xk1 = self.step(tk1, xk1)
            (
                q_dotk1,
                u_dotk1,
                kappa_gk1,
                La_gk1,
                la_gk1,
                la_gammak1,
                kappa_Nk1,
                La_Nk1,
                la_Nk1,
            ) = self.unpack(xk1)

            # update progress bar and check convergence
            pbar.set_description(
                f"t: {tk1:0.2e}s < {self.t1:0.2e}s; Newton: {n_iter}/{self.max_iter} iterations; error: {error:0.2e}"
            )
            if not converged:
                raise RuntimeError(
                    f"internal Newton-Raphson method not converged after {n_iter} steps with error: {error:.5e}"
                )

            # update dependent variables
            y_dotk1 = xk1[: self.ny]
            qk1, uk1, q_dotk1, u_dotk1 = self.update(y_dotk1, store=True)

            # modify converged quantities
            qk1, uk1 = self.model.step_callback(tk1, qk1, uk1)

            # store soltuion fields
            t.append(tk1)
            q.append(qk1)
            u.append(uk1)
            q_dot.append(q_dotk1)
            u_dot.append(u_dotk1)
            kappa_g.append(kappa_gk1)
            La_g.append(La_gk1)
            la_g.append(la_gk1)
            la_gamma.append(la_gammak1)
            kappa_N.append(kappa_Nk1)
            La_N.append(La_Nk1)
            la_N.append(la_Nk1)

            # update local variables for accepted time step
            self.tk = tk1
            self.xk = xk1.copy()

        # write solution
        return Solution(
            t=np.array(t),
            q=np.array(q),
            u=np.array(u),
            q_dot=np.array(q_dot),
            u_dot=np.array(u_dot),
            kappa_g=np.array(kappa_g),
            La_g=np.array(La_g),
            la_g=np.array(la_g),
            la_gamma=np.array(la_gamma),
            kappa_N=np.array(kappa_N),
            La_N=np.array(La_N),
            la_N=np.array(la_N),
        )


class GenAlphaFirstOrderVelocityGGL:
    """Generalized alpha solver for first order DAE's with GLL stabilization.
    
    To-Do:
    -----
    * Think about preconditioning according to Arnold2008 and Bottasso2008?

    References
    ----------
    Arnold2008: https://doi.org/10.1007/s11044-007-9084-0 \\
    Bottasso2008 https://doi.org/10.1007/s11044-007-9051-9
    """

    def __init__(
        self,
        model,
        t1,
        dt,
        rho_inf=1,
        tol=1e-8,
        max_iter=40,
        error_function=lambda x: np.max(np.abs(x)),
        numerical_jacobian=True,
    ):  # ,
        #  use_preconditioning=True):

        self.model = model

        #######################################################################
        # integration time
        #######################################################################
        self.t0 = t0 = model.t0
        self.t1 = (
            t1 if t1 > t0 else ValueError("t1 must be larger than initial time t0.")
        )
        self.dt = dt

        #######################################################################
        # gen alpha parameter
        #######################################################################
        self.rho_inf = rho_inf
        # self.alpha_m = 0.5 * (3. - rho_inf) / (1. + rho_inf) # Jansen2000 (23)
        # self.alpha_f = 1. / (1. + rho_inf) # Jansen2000 (23)
        # self.gamma = 0.5 + self.alpha_m - self.alpha_f # Jansen2000 (17)

        # self.alpha_m = (2. * rho_inf - 1.) / (rho_inf + 1.) # Arnold2007 (24)
        # self.alpha_f = rho_inf / (rho_inf + 1.) # Arnold2007 (24)

        self.alpha_m = (3.0 * rho_inf - 1.0) / (2.0 * (rho_inf + 1.0))  # Harsch2022
        self.alpha_f = rho_inf / (rho_inf + 1.0)  # Harsch2022

        # # third order criterion of Giuseppe
        # # 2.0 * self.alpha_f**2 - self.alpha_f + 1.0 / 6.0 - (2.0 * self.alpha_f - 1.0) * self.alpha_m = 0
        # self.alpha_m = (2.0 * self.alpha_f**2 - self.alpha_f + 1.0 / 6.0) / (2.0 * self.alpha_f - 1.0)
        # # from scipy.optimize import fsolve
        # # func = lambda alpha_f: 2.0 * alpha_f**2 - alpha_f + 1.0 / 6.0 - (2.0 * alpha_f - 1.0) * self.alpha_m
        # # self.alpha_f = fsolve(func, x0=[0.45])[0]

        self.gamma = 0.5 + self.alpha_f - self.alpha_m  # Arnold2007 (24)

        self.gamma_prime = dt * self.gamma * (1.0 - self.alpha_f) / (1.0 - self.alpha_m)

        #######################################################################
        # newton settings
        #######################################################################
        self.tol = tol
        self.max_iter = max_iter
        self.error_function = error_function

        #######################################################################
        # dimensions
        #######################################################################
        self.nq = model.nq
        self.nu = model.nu
        self.nla_g = model.nla_g
        self.nla_gamma = model.nla_gamma
        self.ny = self.nq + self.nu  # dimension of the state space
        self.nx = self.ny + 2 * self.nla_g + self.nla_gamma

        #######################################################################
        # consistent initial conditions
        #######################################################################
        # TODO: Adapt this as done in velocity formulation!
        self.tk = model.t0
        self.qk = model.q0
        self.uk = model.u0
        self.la_gk = model.la_g0
        self.la_gammak = model.la_gamma0
        self.mu_gk = np.zeros(self.nla_g)  # GGL stabilization for position constraints

        # initial velocites
        self.q_dotk = self.model.q_dot(self.tk, self.qk, self.uk)

        # solve for consistent initial accelerations and Lagrange mutlipliers
        M0 = self.model.M(self.tk, self.qk, scipy_matrix=csr_matrix)
        h0 = self.model.h(self.tk, self.qk, self.uk)
        W_g0 = self.model.W_g(self.tk, self.qk, scipy_matrix=csr_matrix)
        W_gamma0 = self.model.W_gamma(self.tk, self.qk, scipy_matrix=csr_matrix)
        zeta_g0 = self.model.zeta_g(t0, self.qk, self.uk)
        zeta_gamma0 = self.model.zeta_gamma(t0, self.qk, self.uk)
        A = bmat(
            [[M0, -W_g0, -W_gamma0], [W_g0.T, None, None], [W_gamma0.T, None, None]],
            format="csc",
        )
        b = np.concatenate([h0, -zeta_g0, -zeta_gamma0])
        u_dot_la_g_la_gamma = spsolve(A, b)
        self.u_dotk = u_dot_la_g_la_gamma[: self.nu]
        self.la_gk = u_dot_la_g_la_gamma[self.nu : self.nu + self.nla_g]
        self.la_gammak = u_dot_la_g_la_gamma[self.nu + self.nla_g :]

        # check if initial conditions satisfy constraints on position, velocity
        # and acceleration level
        g0 = model.g(self.tk, self.qk)
        g_dot0 = model.g_dot(self.tk, self.qk, self.uk)
        g_ddot0 = model.g_ddot(self.tk, self.qk, self.uk, self.u_dotk)
        gamma0 = model.gamma(self.tk, self.qk, self.uk)
        gamma_dot0 = model.gamma_dot(self.tk, self.qk, self.uk, self.u_dotk)

        assert np.allclose(
            g0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g0!"
        assert np.allclose(
            g_dot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_dot0!"
        assert np.allclose(
            g_ddot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_ddot0!"
        assert np.allclose(
            gamma0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma0!"
        assert np.allclose(
            gamma_dot0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma_dot0!"

        #######################################################################
        # starting values for generalized state vector, its derivatives and
        # auxiliary velocities
        #######################################################################
        self.yk = np.concatenate((self.qk, self.uk))
        self.y_dotk = np.concatenate((self.q_dotk, self.u_dotk))
        self.vk = self.y_dotk.copy()  # TODO: Is there a better choice?
        self.xk = np.concatenate(
            (self.q_dotk, self.u_dotk, self.la_gk, self.la_gammak, self.mu_gk)
        )
        # # TODO: Remove this again if it is a bad idea!
        # self.sk = np.concatenate((self.qk, self.uk,
        #                           dt * self.la_gk,
        #                           dt * self.la_gammak,
        #                           dt * self.mu_gk))
        # self.s_dotk = np.concatenate((self.q_dotk, self.u_dotk,
        #                               self.la_gk,
        #                               self.la_gammak,
        #                               self.mu_gk))
        # self.vk = self.s_dotk.copy() # TODO: Is there a better choice?
        # self.xk = self.sk.copy()

        if numerical_jacobian:
            self.__R_gen = self.__R_gen_num
        else:
            self.__R_gen = self.__R_gen_analytic

        # usage of a right preconditioner, see Arnold2008 and Bottasso2008
        use_preconditioning = False
        self.use_preconditioning = use_preconditioning
        if use_preconditioning:
            # fmt: off
            self.D_L = bmat([[eye(self.nq), None, None, None, None],
                             [None, dt * eye(self.nu), None, None, None],
                             [None, None, eye(self.nla_g), None, None],
                             [None, None, None, eye(self.nla_gamma), None],
                             [None, None, None, None, eye(self.nla_g) / dt]])

            # self.D_L = bmat([[eye(self.nq) / self.gamma_prime, None, None, None, None],
            #                  [None, eye(self.nu) / self.gamma_prime, None, None, None],
            #                  [None, None, eye(self.nla_g), None, None],
            #                  [None, None, None, eye(self.nla_gamma), None],
            #                  [None, None, None, None, eye(self.nla_g)]])

            # dt2 = dt**2
            # self.D_R = bmat([[eye(self.nq) / dt2, None, None, None, None],
            #                  [None, eye(self.nu) / dt2, None, None, None],
            #                  [None, None, eye(self.nla_g) / dt2, None, None],
            #                  [None, None, None, eye(self.nla_gamma) / dt2, None],
            #                  [None, None, None, None, eye(self.nla_g) / dt]])

            # self.D_R = bmat([[eye(self.nq) / dt, None, None, None, None],
            #                  [None, eye(self.nu) / dt, None, None, None],
            #                  [None, None, eye(self.nla_g), None, None],
            #                  [None, None, None, eye(self.nla_gamma), None],
            #                  [None, None, None, None, eye(self.nla_g)]])

            # self.D_R = bmat([[eye(self.nq) / self.gamma_prime, None, None, None, None],
            #                  [None, eye(self.nu) / self.gamma_prime, None, None, None],
            #                  [None, None, eye(self.nla_g) / self.gamma_prime, None, None],
            #                  [None, None, None, eye(self.nla_gamma) / self.gamma_prime, None],
            #                  [None, None, None, None, eye(self.nla_g) / self.gamma_prime]])
            # fmt: on

    def update(self, xk1, store=False):
        """Update dependent variables modifed version of Arnold2008 (3) and (5)."""
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        # constants
        dt = self.dt
        gamma = self.gamma
        alpha_f = self.alpha_f
        alpha_m = self.alpha_m

        # auxiliary variables, see Arnold2008 (3)
        y_dotk1 = xk1[: self.ny]
        vk1 = (
            alpha_f * self.y_dotk + (1.0 - alpha_f) * y_dotk1 - alpha_m * self.vk
        ) / (1.0 - alpha_m)

        # approximation of the single integral, see Arnold2008 (5)
        yk1 = self.yk + dt * ((1.0 - gamma) * self.vk + gamma * vk1)

        if store:
            self.vk = vk1.copy()
            self.yk = yk1.copy()
            self.y_dotk = y_dotk1.copy()

        # extract generaliezd coordinates and velocities
        qk1 = yk1[:nq]
        uk1 = yk1[nq : nq + nu]
        q_dotk1 = y_dotk1[:nq]
        u_dotk1 = y_dotk1[nq : nq + nu]
        return qk1, uk1, q_dotk1, u_dotk1

    # def update(self, s_dotk1, store=False):
    #     """Update dependent variables modifed version of Arnold2008 (3) and (5).
    #     """
    #     nq = self.nq
    #     nu = self.nu
    #     nla_g = self.nla_g
    #     nla_gamma = self.nla_gamma

    #     # constants
    #     dt = self.dt
    #     gamma = self.gamma
    #     alpha_f = self.alpha_f
    #     alpha_m = self.alpha_m

    #     # auxiliary variables, see Arnold2008 (3)
    #     vk1 = (alpha_f * self.s_dotk + (1. - alpha_f) * s_dotk1 - alpha_m * self.vk) / (1. - alpha_m)

    #     # approximation of the single integral, see Arnold2008 (5)
    #     sk1 = self.sk + dt * ((1. - gamma) * self.vk + gamma * vk1)

    #     if store:
    #         self.vk = vk1.copy()
    #         self.sk = sk1.copy()
    #         self.s_dotk = s_dotk1.copy()

    #     # extract generaliezd coordinates and velocities
    #     qk1 = sk1[:nq]
    #     uk1 = sk1[nq:nq+nu]
    #     la_gk1 = sk1[nq+nu:nq+nu+nla_g]
    #     la_gammak1 = sk1[nq+nu+nla_g:nq+nu+nla_g+nla_gamma]
    #     mu_gk1 = sk1[nq+nu+nla_g+nla_gamma:nq+nu+2*nla_g+nla_gamma]
    #     q_dotk1 = s_dotk1[:nq]
    #     u_dotk1 = s_dotk1[nq:nq+nu]
    #     return qk1, uk1, la_gk1, la_gammak1, mu_gk1, q_dotk1, u_dotk1

    def pack(self, q_dot, u_dot, la_g, la_gamma, mu_g):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        x = np.zeros(self.nx)

        x[:nq] = q_dot
        x[nq : nq + nu] = u_dot
        x[nq + nu : nq + nu + nla_g] = la_g
        x[nq + nu + nla_g : nq + nu + nla_g + nla_gamma] = la_gamma
        x[nq + nu + nla_g + nla_gamma : nq + nu + 2 * nla_g + nla_gamma] = mu_g

        return x

    def unpack(self, x):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        q_dot = x[:nq]
        u_dot = x[nq : nq + nu]
        la_g = x[nq + nu : nq + nu + nla_g]
        la_gamma = x[nq + nu + nla_g : nq + nu + nla_g + nla_gamma]
        mu_gamma = x[nq + nu + nla_g + nla_gamma : nq + nu + 2 * nla_g + nla_gamma]

        return q_dot, u_dot, la_g, la_gamma, mu_gamma

    def __R_gen_num(self, tk1, xk1):
        yield self.__R(tk1, xk1)
        yield csr_matrix(self.__R_x_num(tk1, xk1))

    def __R_gen_analytic(self, tk1, xk1):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        # extract Lagrange multipliers
        la_gk1 = xk1[self.ny : self.ny + nla_g]
        la_gammak1 = xk1[self.ny + nla_g : nq + nu + nla_g + nla_gamma]
        mu_gk1 = xk1[nq + nu + nla_g + nla_gamma :]

        # update dependent variables
        qk1, uk1, q_dotk1, u_dotk1 = self.update(xk1, store=False)
        # qk1, uk1, la_gk1, la_gammak1, mu_gk1, q_dotk1, u_dotk1 = self.update(xk1, store=False)

        # evaluate repeatedly used quantities
        Mk1 = self.model.M(tk1, qk1)
        W_gk1 = self.model.W_g(tk1, qk1)
        W_gammak1 = self.model.W_gamma(tk1, qk1)

        ###################
        # evaluate residual
        ###################
        R = np.zeros(self.nx)

        # kinematic differential equation
        # Bk1 = self.model.B(tk1, qk1)
        # R[:nq] = q_dotk1 - self.model.q_dot(tk1, qk1, uk1) - Bk1 @ W_gk1 @ mu_gk1
        g_q = self.model.g_q(tk1, qk1)
        R[:nq] = q_dotk1 - self.model.q_dot(tk1, qk1, uk1) - g_q.T @ mu_gk1

        # equations of motion
        R[nq : nq + nu] = (
            Mk1 @ u_dotk1
            - self.model.h(tk1, qk1, uk1)
            - W_gk1 @ la_gk1
            - W_gammak1 @ la_gammak1
        )

        R[nq + nu : nq + nu + nla_g] = self.model.g_dot(tk1, qk1, uk1)
        R[nq + nu + nla_g : nq + nu + nla_g + nla_gamma] = self.model.gamma(
            tk1, qk1, uk1
        )
        R[nq + nu + nla_g + nla_gamma :] = self.model.g(tk1, qk1)

        yield R

        raise RuntimeError("...")

    def __R(self, tk1, xk1):
        return next(self.__R_gen_analytic(tk1, xk1))

    def __R_x_num(self, tk1, xk1):
        return csr_matrix(
            approx_fprime(xk1, lambda x: self.__R(tk1, x), method="2-point")
        )

    def step(self, tk1, xk1):
        # initial residual and error
        R_gen = self.__R_gen(tk1, xk1)
        R = next(R_gen)
        error = self.error_function(R)
        converged = error < self.tol
        j = 0
        if not converged:
            while j < self.max_iter:
                # jacobian
                R_x = next(R_gen)

                # Newton update
                j += 1
                if self.use_preconditioning:
                    raise NotImplementedError("Not correct yet!")
                    # TODO: It this efficient? Blas level 3 and blas level 2
                    #       operation shouldn't be that bad for sparse
                    #       matrices.
                    # dx = spsolve(self.D_L @ R_x @ self.D_R, self.D_L @ R, use_umfpack=True)
                    # xk1 -= self.D_R @ dx
                    # dx = spsolve(R_x @ self.D_R, R, use_umfpack=True)
                    # xk1 -= self.D_R @ dx
                    dx = spsolve(self.D_L @ R_x, self.D_L @ R, use_umfpack=True)
                    xk1 -= dx
                else:
                    dx = spsolve(R_x, R, use_umfpack=True)
                    xk1 -= dx
                R_gen = self.__R_gen(tk1, xk1)
                R = next(R_gen)

                error = self.error_function(R)
                converged = error < self.tol
                if converged:
                    break

        return converged, j, error, xk1

    def solve(self):
        # lists storing output variables
        t = [self.tk]
        q = [self.qk]
        u = [self.uk]
        q_dot = [self.q_dotk]
        u_dot = [self.u_dotk]
        la_g = [self.la_gk]
        la_gamma = [self.la_gammak]
        mu_g = [self.mu_gk]

        pbar = tqdm(np.arange(self.t0, self.t1, self.dt))
        for _ in pbar:
            # perform a sovler step
            tk1 = self.tk + self.dt
            xk1 = self.xk.copy()  # This copy is mandatory since we modify xk1
            # in the step function
            converged, n_iter, error, xk1 = self.step(tk1, xk1)
            q_dotk1, u_dotk1, la_gk1, la_gammak1, mu_gk1 = self.unpack(xk1)

            # update progress bar and check convergence
            pbar.set_description(
                f"t: {tk1:0.2e}s < {self.t1:0.2e}s; Newton: {n_iter}/{self.max_iter} iterations; error: {error:0.2e}"
            )
            if not converged:
                raise RuntimeError(
                    f"internal Newton-Raphson method not converged after {n_iter} steps with error: {error:.5e}"
                )

            # update dependent variables
            qk1, uk1, q_dotk1, u_dotk1 = self.update(xk1, store=True)
            # qk1, uk1, la_gk1, la_gammak1, mu_gk1, q_dotk1, u_dotk1 = self.update(xk1, store=True)

            # modify converged quantities
            qk1, uk1 = self.model.step_callback(tk1, qk1, uk1)

            # store soltuion fields
            t.append(tk1)
            q.append(qk1)
            u.append(uk1)
            q_dot.append(q_dotk1)
            u_dot.append(u_dotk1)
            la_g.append(la_gk1)
            la_gamma.append(la_gammak1)
            mu_g.append(mu_gk1)

            # update local variables for accepted time step
            self.tk = tk1
            self.xk = xk1.copy()

        # write solution
        return Solution(
            t=np.array(t),
            q=np.array(q),
            u=np.array(u),
            q_dot=np.array(q_dot),
            u_dot=np.array(u_dot),
            la_g=np.array(la_g),
            la_gamma=np.array(la_gamma),
            mu_g=np.array(mu_g),
        )


class GenAlphaFirstOrderVelocity:
    """Generalized alpha solver for first order ODE's.
    
    To-Do:
    -----
    * Add GGL stabilization for constraints on position level in kinematic 
      differential equation in order to solve an index 2 DAE system 
    * Think about preconditioning according to Arnold2008 and Bottasso2008?

    References
    ----------
    Arnold2008: https://doi.org/10.1007/s11044-007-9084-0 \\
    Bottasso2008 https://doi.org/10.1007/s11044-007-9051-9
    """

    def __init__(
        self,
        model,
        t1,
        dt,
        rho_inf=1,
        tol=1e-8,
        max_iter=40,
        error_function=lambda x: np.max(np.abs(x)),
        numerical_jacobian=True,
        DAE_index=3,
    ):

        self.model = model
        assert DAE_index >= 1 and DAE_index <= 3, "DAE_index hast to be in [1, 3]!"
        self.DAE_index = DAE_index

        #######################################################################
        # integration time
        #######################################################################
        self.t0 = t0 = model.t0
        self.t1 = (
            t1 if t1 > t0 else ValueError("t1 must be larger than initial time t0.")
        )
        self.dt = dt

        #######################################################################
        # gen alpha parameter
        #######################################################################
        self.rho_inf = rho_inf
        self.alpha_m = (3.0 * rho_inf - 1.0) / (2.0 * (rho_inf + 1.0))  # Harsch2022
        self.alpha_f = rho_inf / (rho_inf + 1.0)  # Harsch2022
        self.gamma = 0.5 + self.alpha_f - self.alpha_m  # Arnold2007 (24)
        # self.q_q_dot = ???
        # self.u_udot = ???
        self.x_x_dot = (
            self.dt * self.gamma * (1.0 - self.alpha_f) / (1.0 - self.alpha_m)
        )

        #######################################################################
        # newton settings
        #######################################################################
        self.tol = tol
        self.max_iter = max_iter
        self.error_function = error_function

        #######################################################################
        # dimensions
        #######################################################################
        self.nq = model.nq
        self.nu = model.nu
        self.nla_g = model.nla_g
        self.nla_gamma = model.nla_gamma
        self.ny = self.nq + self.nu  # dimension of the state space
        self.nx = self.ny + self.nla_g + self.nla_gamma

        if numerical_jacobian:
            self.__R_gen = self.__R_gen_num
        else:
            self.__R_gen = self.__R_gen_analytic

        #######################################################################
        # consistent initial conditions
        #######################################################################

        # def initial_values_Arnold2014(t0, q0, u0, h, s=1, order=2):
        #     assert order in [1, 2], "order hast to be in [1, 2]!"

        #     # initial velocites
        #     q_dot0 = self.model.q_dot(t0, q0, u0)

        #     # solve for consistent initial accelerations and Lagrange mutlipliers
        #     M0 = self.model.M(t0, q0, scipy_matrix=csr_matrix)
        #     h0 = self.model.h(t0, q0, u0)
        #     W_g0 = self.model.W_g(t0, q0, scipy_matrix=csr_matrix)
        #     W_gamma0 = self.model.W_gamma(t0, q0, scipy_matrix=csr_matrix)
        #     zeta_g0 = self.model.zeta_g(t0, q0, u0)
        #     zeta_gamma0 = self.model.zeta_gamma(t0, q0, u0)
        #     A = bmat(
        #         [
        #             [M0, -W_g0, -W_gamma0],
        #             [W_g0.T, None, None],
        #             [W_gamma0.T, None, None],
        #         ],
        #         format="csc",
        #     )
        #     b = np.concatenate([h0, -zeta_g0, -zeta_gamma0])
        #     tmp = spsolve(A, b)
        #     u_dot0 = tmp[: self.nu]
        #     la_g0 = tmp[self.nu : self.nu + self.nla_g]
        #     la_gamma0 = tmp[self.nu + self.nla_g :]

        #     self.yk = np.concatenate((q0, u0))
        #     self.y_dotk = np.concatenate((q_dot0, u_dot0))
        #     # self.vk = self.y_dotk.copy() # TODO: Is there a better choice?
        #     self.xk = self.pack(q_dot0, u_dot0, la_g0, la_gamma0)

        #     vk = self.y_dotk.copy()
        #     return q_dot0, u_dot0, la_g0, la_gamma0, vk

        #     # # TODO: This is not correct yet!
        #     # if order == 2:
        #     #     # solve residual for forward step
        #     #     ts_p = t0 + s * h
        #     #     # xs_p = self.pack(q_dot0, u_dot0, la_g0, la_gamma0)
        #     #     # converged, j, error, xs_p = self.step(ts_p, xs_p)
        #     #     qs_p = q0 + s * h * q_dot0
        #     #     us_p = u0 + s * h * u_dot0

        #     #     # solve residual for backward step
        #     #     ts_m = t0 - s * h
        #     #     # xs_m = self.pack(q_dot0, u_dot0, la_g0, la_gamma0)
        #     #     # converged, j, error, xs_m = self.step(ts_m, xs_m)
        #     #     qs_m = q0 - s * h * q_dot0
        #     #     us_m = u0 - s * h * u_dot0

        #     #     # v0 = (xs_p - xs_m) / (2 * s * h)
        #     #     vs_p = np.concatenate((qs_p, us_p))
        #     #     vs_m = np.concatenate((qs_m, us_m))
        #     #     delta_alpha = self.alpha_m - self.alpha_f # Arnold2015 (41)
        #     #     v0 = self.y_dotk + delta_alpha * h * (vs_p - vs_m) / (2 * s * h) # Arnold2015 Table 1
        #     # else:
        #     #     raise NotImplementedError("")
        #     #     # # solve residual for forward step
        #     #     # ts_p = t0 + s * h
        #     #     # xs_p = self.pack(q_dot0, u_dot0, la_g0, la_gamma0)
        #     #     # converged, j, error, xs_p = self.step(ts_p, xs_p)

        #     #     # v0 = (xs_p - self.xk) / (s * h)

        #     # return q_dot0, u_dot0, la_g0, la_gamma0, v0

        def initial_values(t0, q0, u0):
            # initial velocites
            q_dot0 = self.model.q_dot(t0, q0, u0)

            # solve for consistent initial accelerations and Lagrange mutlipliers
            M0 = self.model.M(t0, q0, scipy_matrix=csr_matrix)
            h0 = self.model.h(t0, q0, u0)
            W_g0 = self.model.W_g(t0, q0, scipy_matrix=csr_matrix)
            W_gamma0 = self.model.W_gamma(t0, q0, scipy_matrix=csr_matrix)
            zeta_g0 = self.model.zeta_g(t0, q0, u0)
            zeta_gamma0 = self.model.zeta_gamma(t0, q0, u0)
            A = bmat(
                [
                    [M0, -W_g0, -W_gamma0],
                    [W_g0.T, None, None],
                    [W_gamma0.T, None, None],
                ],
                format="csc",
            )
            b = np.concatenate([h0, -zeta_g0, -zeta_gamma0])
            u_dot_la_g_la_gamma = spsolve(A, b)
            u_dot0 = u_dot_la_g_la_gamma[: self.nu]
            la_g0 = u_dot_la_g_la_gamma[self.nu : self.nu + self.nla_g]
            la_gamma0 = u_dot_la_g_la_gamma[self.nu + self.nla_g :]

            y0 = np.concatenate((q0, u0))
            y_dot0 = np.concatenate((q_dot0, u_dot0))
            v0 = y_dot0.copy()  # TODO: Is there a better choice?
            x0 = self.pack(q_dot0, u_dot0, la_g0, la_gamma0)

            return t0, q0, u0, q_dot0, u_dot0, v0, la_g0, la_gamma0, x0, y0, y_dot0

        # compute consistent initial conditions
        (
            self.tk,
            self.qk,
            self.uk,
            self.q_dotk,
            self.u_dotk,
            self.vk,
            self.la_gk,
            self.la_gammak,
            self.xk,
            self.yk,
            self.y_dotk,
        ) = initial_values(t0, model.q0, model.u0)
        # TODO:
        # ) = initial_values_Arnold2014(self.tk, self.qk, self.uk, dt)

        # check if initial conditions satisfy constraints on position, velocity
        # and acceleration level
        g0 = model.g(self.tk, self.qk)
        g_dot0 = model.g_dot(self.tk, self.qk, self.uk)
        g_ddot0 = model.g_ddot(self.tk, self.qk, self.uk, self.u_dotk)
        gamma0 = model.gamma(self.tk, self.qk, self.uk)
        gamma_dot0 = model.gamma_dot(self.tk, self.qk, self.uk, self.u_dotk)

        assert np.allclose(
            g0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g0!"
        assert np.allclose(
            g_dot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_dot0!"
        assert np.allclose(
            g_ddot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_ddot0!"
        assert np.allclose(
            gamma0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma0!"
        assert np.allclose(
            gamma_dot0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma_dot0!"

    def update(self, y_dotk1, store=False):
        """Update dependent variables modifed version of Arnold2008 (3) and (5)."""
        nq = self.nq
        nu = self.nu

        # constants
        dt = self.dt
        gamma = self.gamma
        alpha_f = self.alpha_f
        alpha_m = self.alpha_m

        # auxiliary variables, see Arnold2008 (3)
        vk1 = (
            alpha_f * self.y_dotk + (1.0 - alpha_f) * y_dotk1 - alpha_m * self.vk
        ) / (1.0 - alpha_m)

        # approximation of the single integral, see Arnold2008 (5)
        yk1 = self.yk + dt * ((1.0 - gamma) * self.vk + gamma * vk1)

        if store:
            self.vk = vk1.copy()
            self.yk = yk1.copy()
            self.y_dotk = y_dotk1.copy()

        # extract generaliezd coordinates and velocities
        qk1 = yk1[:nq]
        uk1 = yk1[nq : nq + nu]
        q_dotk1 = y_dotk1[:nq]
        u_dotk1 = y_dotk1[nq : nq + nu]
        return qk1, uk1, q_dotk1, u_dotk1

    def pack(self, q_dot, u_dot, la_g, la_gamma):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g

        x = np.zeros(self.nx)

        x[:nq] = q_dot
        x[nq : nq + nu] = u_dot
        x[nq + nu : nq + nu + nla_g] = la_g
        x[nq + nu + nla_g :] = la_gamma

        return x

    def unpack(self, x):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g

        q_dot = x[:nq]
        u_dot = x[nq : nq + nu]
        la_g = x[nq + nu : nq + nu + nla_g]
        la_gamma = x[nq + nu + nla_g :]

        return q_dot, u_dot, la_g, la_gamma

    def __R_gen_num(self, tk1, xk1):
        yield self.__R(tk1, xk1)
        yield csr_matrix(self.__R_x_num(tk1, xk1))

    def __R_gen_analytic(self, tk1, xk1):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g

        # extract auxiliary velocities and Lagrange multipliers
        y_dotk1 = xk1[: self.ny]
        la_gk1 = xk1[self.ny : self.ny + nla_g]
        la_gammak1 = xk1[self.ny + nla_g :]

        # update dependent variables
        qk1, uk1, q_dotk1, u_dotk1 = self.update(y_dotk1, store=False)

        ###################
        # evaluate residual
        ###################
        R = np.zeros(self.nx)

        # kinematic differential equation
        R[:nq] = q_dotk1 - self.model.q_dot(tk1, qk1, uk1)

        # equations of motion
        R[nq : nq + nu] = (
            self.model.M(tk1, qk1) @ u_dotk1
            - self.model.h(tk1, qk1, uk1)
            - self.model.W_g(tk1, qk1) @ la_gk1
            - self.model.W_gamma(tk1, qk1) @ la_gammak1
        )

        if self.DAE_index == 3:
            R[nq + nu : nq + nu + nla_g] = self.model.g(tk1, qk1)
            R[nq + nu + nla_g :] = self.model.gamma(tk1, qk1, uk1)
        elif self.DAE_index == 2:
            R[nq + nu : nq + nu + nla_g] = self.model.g_dot(tk1, qk1, uk1)
            R[nq + nu + nla_g :] = self.model.gamma(tk1, qk1, uk1)
        elif self.DAE_index == 1:
            R[nq + nu : nq + nu + nla_g] = self.model.g_ddot(tk1, qk1, uk1, u_dotk1)
            R[nq + nu + nla_g :] = self.model.gamma_dot(tk1, qk1, uk1, u_dotk1)

        yield R

        #################################
        # kinematic differential equation
        #################################
        # R[:nq] = q_dotk1 - self.model.q_dot(tk1, qk1, uk1)
        Rq_q_dot = eye(self.nq) - self.model.q_dot_q(tk1, qk1, uk1) * self.x_x_dot
        Rq_u_dot = self.model.B(tk1, qk1) * self.x_x_dot
        Rq_la_g = None
        Rq_la_gamma = None

        #####################
        # equations of motion
        #####################
        # R[nq : nq + nu] = (
        #     self.model.M(tk1, qk1) @ u_dotk1
        #     - self.model.h(tk1, qk1, uk1)
        #     - self.model.W_g(tk1, qk1) @ la_gk1
        #     - self.model.W_gamma(tk1, qk1) @ la_gammak1
        # )
        R_u_q_dot = (
            self.model.Mu_q(tk1, qk1, u_dotk1)
            - self.model.h_q(tk1, qk1, uk1)
            - self.model.Wla_g_q(tk1, qk1, la_gk1)
            - self.model.Wla_gamma_q(tk1, qk1, la_gammak1)
        ) * self.x_x_dot
        R_u_u_dot = (
            self.model.M(tk1, qk1) - self.model.h_u(tk1, qk1, uk1) * self.x_x_dot
        )
        Ru_la_g = None
        Ru_la_gamma = None

        ###############################################################################################
        # R[:nu] = Mk1 @ ak1 -( self.model.h(tk1, qk1, uk1) + W_gk1 @ la_gk1 + W_gammak1 @ la_gammak1 )
        ###############################################################################################
        Wla_g_q = self.model.Wla_g_q(tk1, qk1, la_gk1, scipy_matrix=csr_matrix)
        Wla_gamma_q = self.model.Wla_gamma_q(
            tk1, qk1, la_gammak1, scipy_matrix=csr_matrix
        )
        rhs_q = -(self.model.h_q(tk1, qk1, uk1) + Wla_g_q + Wla_gamma_q)
        rhs_u = -self.model.h_u(tk1, qk1, uk1)
        rhs_a = rhs_q @ self.q_a + rhs_u * self.u_a
        Ma_a = (
            self.model.Mu_q(tk1, qk1, ak1, scipy_matrix=csr_matrix) @ self.q_a + rhs_a
        )

        Ra_a = Mk1 + Ma_a
        Ra_la_g = -W_gk1
        Ra_la_g *= self.pre_cond
        Ra_la_gamma = -W_gammak1

        #########################################
        # R[nu:nu+nla_g] = self.model.g(tk1, qk1)
        #########################################
        Rla_g_a = self.model.g_q(tk1, qk1) @ self.q_a
        Rla_g_la_g = None
        Rla_g_la_gamma = None

        ##################################################################
        # R[nu+nla_g:nu+nla_g+nla_gamma] = self.model.gamma(tk1, qk1, uk1)
        ##################################################################
        Rla_gamma_a = (
            self.model.gamma_q(tk1, qk1, uk1) @ self.q_a
            + self.model.gamma_u(tk1, qk1) * self.u_a
        )
        Rla_gamma_la_g = None
        Rla_gamma_la_gamma = None

        # sparse assemble global tangent matrix
        R_x = bmat(
            [
                [Ra_a, Ra_la_g, Ra_la_gamma],
                [Rla_g_a, Rla_g_la_g, Rla_g_la_gamma],
                [Rla_gamma_a, Rla_gamma_la_g, Rla_gamma_la_gamma],
            ],
            format="csr",
        )

        yield R_x

    def __R(self, tk1, xk1):
        return next(self.__R_gen_analytic(tk1, xk1))

    def __R_x_num(self, tk1, xk1):
        return csr_matrix(
            approx_fprime(xk1, lambda x: self.__R(tk1, x), method="2-point")
        )

    def step(self, tk1, xk1):
        # initial residual and error
        R_gen = self.__R_gen(tk1, xk1)
        R = next(R_gen)
        error = self.error_function(R)
        converged = error < self.tol
        j = 0
        if not converged:
            while j < self.max_iter:
                # jacobian
                R_x = next(R_gen)

                # Newton update
                j += 1
                dx = spsolve(R_x, R, use_umfpack=True)
                xk1 -= dx
                R_gen = self.__R_gen(tk1, xk1)
                R = next(R_gen)

                error = self.error_function(R)
                converged = error < self.tol
                if converged:
                    break

        return converged, j, error, xk1

    def solve(self):
        # lists storing output variables
        t = [self.tk]
        q = [self.qk]
        u = [self.uk]
        q_dot = [self.q_dotk]
        u_dot = [self.u_dotk]
        la_g = [self.la_gk]
        la_gamma = [self.la_gammak]

        pbar = tqdm(np.arange(self.t0, self.t1, self.dt))
        for _ in pbar:
            # perform a sovler step
            tk1 = self.tk + self.dt
            xk1 = self.xk.copy()  # This copy is mandatory since we modify xk1
            # in the step function
            converged, n_iter, error, xk1 = self.step(tk1, xk1)
            q_dotk1, u_dotk1, la_gk1, la_gammak1 = self.unpack(xk1)

            # update progress bar and check convergence
            pbar.set_description(
                f"t: {tk1:0.2e}s < {self.t1:0.2e}s; Newton: {n_iter}/{self.max_iter} iterations; error: {error:0.2e}"
            )
            if not converged:
                raise RuntimeError(
                    f"internal Newton-Raphson method not converged after {n_iter} steps with error: {error:.5e}"
                )

            # update dependent variables
            y_dotk1 = xk1[: self.ny]
            qk1, uk1, q_dotk1, u_dotk1 = self.update(y_dotk1, store=True)

            # modify converged quantities
            qk1, uk1 = self.model.step_callback(tk1, qk1, uk1)

            # store soltuion fields
            t.append(tk1)
            q.append(qk1)
            u.append(uk1)
            q_dot.append(q_dotk1)
            u_dot.append(u_dotk1)
            la_g.append(la_gk1)
            la_gamma.append(la_gammak1)

            # update local variables for accepted time step
            self.tk = tk1
            self.xk = xk1.copy()

        # write solution
        return Solution(
            t=np.array(t),
            q=np.array(q),
            u=np.array(u),
            q_dot=np.array(q_dot),
            u_dot=np.array(u_dot),
            la_g=np.array(la_g),
            la_gamma=np.array(la_gamma),
        )


class GenAlphaFirstOrderPosition:
    """Generalized alpha solver for first order ODE's taken from Jansen2000.
    
    To-Do:
    -----
    * Can we adapt this for constrained mechanical systems of DAE 
      index 3 according to Arnold2008 and Bottasso2008?
    * Add GGL stabilization for constraints on position level in kinematic 
      differential equation in order to solve an index 2 DAE system 
    * Think about preconditioning according to Arnold2008 and Bottasso2008?

    References
    ----------
    Jansen2000: https://doi.org/10.1016/S0045-7825(00)00203-6 \\
    Arnold2008: https://doi.org/10.1007/s11044-007-9084-0 \\
    Bottasso2008 https://doi.org/10.1007/s11044-007-9051-9
    """

    def __init__(
        self,
        model,
        t1,
        dt,
        rho_inf=1,
        tol=1e-8,
        max_iter=40,
        error_function=lambda x: np.max(np.abs(x)),
        numerical_jacobian=True,
        DAE_index=3,
    ):

        self.model = model
        assert DAE_index >= 1 and DAE_index <= 3, "DAE_index hast to be in [1, 3]!"
        self.DAE_index = DAE_index

        #######################################################################
        # integration time
        #######################################################################
        self.t0 = t0 = model.t0
        self.t1 = (
            t1 if t1 > t0 else ValueError("t1 must be larger than initial time t0.")
        )
        self.dt = dt

        #######################################################################
        # gen alpha parameter
        #######################################################################
        self.rho_inf = rho_inf
        # self.alpha_m = 0.5 * (3. - rho_inf) / (1. + rho_inf) # Jansen2000 (23)
        # self.alpha_f = 1. / (1. + rho_inf) # Jansen2000 (23)
        # self.gamma = 0.5 + self.alpha_m - self.alpha_f # Jansen2000 (17)

        self.alpha_m = (2.0 * rho_inf - 1.0) / (rho_inf + 1.0)  # Arnold2007 (24)
        self.alpha_f = rho_inf / (rho_inf + 1.0)  # Arnold2007 (24)

        self.alpha_m = (3.0 * rho_inf - 1.0) / (2.0 * (rho_inf + 1.0))  # Harsch2022
        self.alpha_f = rho_inf / (rho_inf + 1.0)  # Harsch2022

        self.gamma = 0.5 + self.alpha_f - self.alpha_m  # Arnold2007 (24)

        #######################################################################
        # newton settings
        #######################################################################
        self.tol = tol
        self.max_iter = max_iter
        self.error_function = error_function

        #######################################################################
        # dimensions
        #######################################################################
        self.nq = model.nq
        self.nu = model.nu
        self.nla_g = model.nla_g
        self.nla_gamma = model.nla_gamma
        self.ny = self.nq + self.nu  # dimension of the state space
        self.nx = self.ny + self.nla_g + self.nla_gamma

        #######################################################################
        # consistent initial conditions
        #######################################################################
        self.tk = model.t0
        self.qk = model.q0
        self.uk = model.u0
        self.la_gk = model.la_g0
        self.la_gammak = model.la_gamma0

        # initial velocites
        self.q_dotk = self.model.q_dot(self.tk, self.qk, self.uk)

        # solve for consistent initial accelerations and Lagrange mutlipliers
        M0 = self.model.M(self.tk, self.qk, scipy_matrix=csr_matrix)
        h0 = self.model.h(self.tk, self.qk, self.uk)
        W_g0 = self.model.W_g(self.tk, self.qk, scipy_matrix=csr_matrix)
        W_gamma0 = self.model.W_gamma(self.tk, self.qk, scipy_matrix=csr_matrix)
        zeta_g0 = self.model.zeta_g(t0, self.qk, self.uk)
        zeta_gamma0 = self.model.zeta_gamma(t0, self.qk, self.uk)
        A = bmat(
            [[M0, -W_g0, -W_gamma0], [W_g0.T, None, None], [W_gamma0.T, None, None]],
            format="csc",
        )
        b = np.concatenate([h0, -zeta_g0, -zeta_gamma0])
        u_dot_la_g_la_gamma = spsolve(A, b)
        self.u_dotk = u_dot_la_g_la_gamma[: self.nu]
        self.la_gk = u_dot_la_g_la_gamma[self.nu : self.nu + self.nla_g]
        self.la_gammak = u_dot_la_g_la_gamma[self.nu + self.nla_g :]

        # check if initial conditions satisfy constraints on position, velocity
        # and acceleration level
        g0 = model.g(self.tk, self.qk)
        g_dot0 = model.g_dot(self.tk, self.qk, self.uk)
        g_ddot0 = model.g_ddot(self.tk, self.qk, self.uk, self.u_dotk)
        gamma0 = model.gamma(self.tk, self.qk, self.uk)
        gamma_dot0 = model.gamma_dot(self.tk, self.qk, self.uk, self.u_dotk)

        assert np.allclose(
            g0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g0!"
        assert np.allclose(
            g_dot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_dot0!"
        assert np.allclose(
            g_ddot0, np.zeros(self.nla_g)
        ), "Initial conditions do not fulfill g_ddot0!"
        assert np.allclose(
            gamma0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma0!"
        assert np.allclose(
            gamma_dot0, np.zeros(self.nla_gamma)
        ), "Initial conditions do not fulfill gamma_dot0!"

        #######################################################################
        # starting values for generalized state vector, its derivatives and
        # auxiliary velocities
        #######################################################################
        self.yk = np.concatenate((self.qk, self.uk))
        self.y_dotk = np.concatenate((self.q_dotk, self.u_dotk))
        self.vk = self.y_dotk.copy()  # TODO: Is there a better choice?
        self.xk = self.pack(self.qk, self.uk, self.la_gk, self.la_gammak)

        if numerical_jacobian:
            self.__R_gen = self.__R_gen_num
        else:
            self.__R_gen = self.__R_gen_analytic

    def update(self, yk1, store=False):
        """Update dependent variables modifed version of Arnold2008 (3) and (5)."""
        nq = self.nq
        nu = self.nu

        # constants
        dt = self.dt
        gamma = self.gamma
        alpha_f = self.alpha_f
        alpha_m = self.alpha_m

        # auxiliary velocities, see Arnold2008 (5)
        vk1 = ((yk1 - self.yk) / dt - (1.0 - gamma) * self.vk) / gamma

        # true velocities, see Arnold2008 (3)
        y_dotk1 = (
            alpha_m * self.vk + (1.0 - alpha_m) * vk1 - alpha_f * self.y_dotk
        ) / (1.0 - alpha_f)

        if store:
            self.vk = vk1.copy()
            self.yk = yk1.copy()
            self.y_dotk = y_dotk1.copy()

        # extract generaliezd coordinates and velocities
        qk1 = yk1[:nq]
        uk1 = yk1[nq : nq + nu]
        q_dotk1 = y_dotk1[:nq]
        u_dotk1 = y_dotk1[nq : nq + nu]
        return qk1, uk1, q_dotk1, u_dotk1

    def pack(self, q, u, la_g, la_gamma):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g

        x = np.zeros(self.nx)

        x[:nq] = q
        x[nq : nq + nu] = u
        x[nq + nu : nq + nu + nla_g] = la_g
        x[nq + nu + nla_g :] = la_gamma

        return x

    def unpack(self, x):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g

        q = x[:nq]
        u = x[nq : nq + nu]
        la_g = x[nq + nu : nq + nu + nla_g]
        la_gamma = x[nq + nu + nla_g :]

        return q, u, la_g, la_gamma

    def __R_gen_num(self, tk1, xk1):
        yield self.__R(tk1, xk1)
        yield csr_matrix(self.__R_x_num(tk1, xk1))

    def __R_gen_analytic(self, tk1, xk1):
        nq = self.nq
        nu = self.nu
        nla_g = self.nla_g

        # extract auxiliary velocities and Lagrange multipliers
        yk1 = xk1[: self.ny]
        la_gk1 = xk1[self.ny : self.ny + nla_g]
        la_gammak1 = xk1[self.ny + nla_g :]

        # update dependent variables
        qk1, uk1, q_dotk1, u_dotk1 = self.update(yk1, store=False)

        ###################
        # evaluate residual
        ###################
        R = np.zeros(self.nx)

        # kinematic differential equation
        R[:nq] = q_dotk1 - self.model.q_dot(tk1, qk1, uk1)

        # equations of motion
        R[nq : nq + nu] = (
            self.model.M(tk1, qk1) @ u_dotk1
            - self.model.h(tk1, qk1, uk1)
            - self.model.W_g(tk1, qk1) @ la_gk1
            - self.model.W_gamma(tk1, qk1) @ la_gammak1
        )

        if self.DAE_index == 3:
            R[nq + nu : nq + nu + nla_g] = self.model.g(tk1, qk1)
            R[nq + nu + nla_g :] = self.model.gamma(tk1, qk1, uk1)
        elif self.DAE_index == 2:
            R[nq + nu : nq + nu + nla_g] = self.model.g_dot(tk1, qk1, uk1)
            R[nq + nu + nla_g :] = self.model.gamma(tk1, qk1, uk1)
        elif self.DAE_index == 1:
            R[nq + nu : nq + nu + nla_g] = self.model.g_ddot(tk1, qk1, uk1, u_dotk1)
            R[nq + nu + nla_g :] = self.model.gamma_dot(tk1, qk1, uk1, u_dotk1)

        yield R

        raise RuntimeError("...")

    def __R(self, tk1, xk1):
        return next(self.__R_gen_analytic(tk1, xk1))

    def __R_x_num(self, tk1, xk1):
        return approx_fprime(xk1, lambda x: self.__R(tk1, x), method="2-point")

    def step(self, tk1, xk1):
        # initial residual and error
        R_gen = self.__R_gen(tk1, xk1)
        R = next(R_gen)
        error = self.error_function(R)
        converged = error < self.tol
        j = 0
        if not converged:
            while j < self.max_iter:
                # jacobian
                R_x = next(R_gen)

                # Newton update
                j += 1
                dx = spsolve(R_x, R, use_umfpack=True)
                xk1 -= dx
                R_gen = self.__R_gen(tk1, xk1)
                R = next(R_gen)

                error = self.error_function(R)
                converged = error < self.tol
                if converged:
                    break

        return converged, j, error, xk1

    def solve(self):
        # lists storing output variables
        # TODO: Get rid of self.qk, self.uk etc. and unpack self.xk here!
        t = [self.tk]
        q = [self.qk]
        u = [self.uk]
        u_dot = [self.u_dotk]
        la_g = [self.la_gk]
        la_gamma = [self.la_gammak]

        pbar = tqdm(np.arange(self.t0, self.t1, self.dt))
        for _ in pbar:
            # perform a sovler step
            tk1 = self.tk + self.dt
            xk1 = self.xk.copy()  # This copy is mandatory since we modify xk1
            # in the step function
            converged, n_iter, error, xk1 = self.step(tk1, xk1)
            qk1, uk1, la_gk1, la_gammak1 = self.unpack(xk1)

            # update progress bar and check convergence
            pbar.set_description(
                f"t: {tk1:0.2e}s < {self.t1:0.2e}s; Newton: {n_iter}/{self.max_iter} iterations; error: {error:0.2e}"
            )
            if not converged:
                raise RuntimeError(
                    f"internal Newton-Raphson method not converged after {n_iter} steps with error: {error:.5e}"
                )

            # update dependent variables
            yk1 = xk1[: self.ny]
            qk1, uk1, q_dotk1, u_dotk1 = self.update(yk1, store=True)

            # modify converged quantities
            qk1, uk1 = self.model.step_callback(tk1, qk1, uk1)

            # store soltuion fields
            t.append(tk1)
            q.append(qk1)
            u.append(uk1)
            u_dot.append(u_dotk1)
            la_g.append(la_gk1)
            la_gamma.append(la_gammak1)

            # update local variables for accepted time step
            self.tk = tk1
            self.xk = xk1.copy()

        # write solution
        return Solution(
            t=np.array(t),
            q=np.array(q),
            u=np.array(u),
            u_dot=np.array(u_dot),
            la_g=np.array(la_g),
            la_gamma=np.array(la_gamma),
        )


class GenAlphaDAEPos:
    """Generalized alpha solver for constraint mechanical systems of DAE 
    index 3 with the right precondtioner introduced in Arnold2008 and 
    Bottasso2008. The (q, lambda) form is implemented here. Aditionally 
    constraints on velocity level can be solved.

    References
    ----------
    Bottasso2008 https://doi.org/10.1007/s11044-007-9051-9 \\
    Arnold2008: https://doi.org/10.1007/s11044-007-9084-0
    """

    def __init__(
        self,
        model,
        t1,
        dt,
        rho_inf=1,
        beta=None,
        gamma=None,
        alpha_m=None,
        alpha_f=None,
        tol=1e-8,
        max_iter=40,
        error_function=lambda x: np.max(np.abs(x)),
        numerical_jacobian=False,
        pre_cond=True,
    ):

        self.model = model

        # integration time
        self.t0 = t0 = model.t0
        self.t1 = (
            t1 if t1 > t0 else ValueError("t1 must be larger than initial time t0.")
        )
        self.dt = dt

        # parameter
        self.rho_inf = rho_inf
        if None in [beta, gamma, alpha_m, alpha_f]:
            self.alpha_m = (2 * rho_inf - 1) / (1 + rho_inf)
            self.alpha_f = rho_inf / (1 + rho_inf)
            self.gamma = 0.5 + self.alpha_f - self.alpha_m
            self.beta = 0.25 * ((self.gamma + 0.5) ** 2)
        else:
            self.gamma = gamma
            self.beta = beta
            self.alpha_m = alpha_m
            self.alpha_f = alpha_f
        self.alpha_ratio = (1 - self.alpha_f) / (1 - self.alpha_m)

        # usage of a right preconditioner, see Arnold2008 and Bottasso2008
        if pre_cond:
            self.pre_cond = 1.0 / (self.dt**2 * self.beta)
        else:
            self.pre_cond = 1  # no preconditioning

        # newton settings
        self.tol = tol
        self.max_iter = max_iter
        self.error_function = error_function

        # dimensions
        self.nq = model.nq
        self.nu = model.nu
        self.nla_g = model.nla_g
        self.nla_gamma = model.nla_gamma

        # equation sof motion, constraints on position level and constraints on velocitiy level
        self.nR = self.nq + self.nla_g + self.nla_gamma

        self.tk = model.t0
        self.qk = model.q0
        self.uk = model.u0
        self.la_gk = model.la_g0
        self.la_gammak = model.la_gamma0

        # TODO: Do we have to compute some initial accelerations for the position based implementation too?
        # if a0 is not None:
        #     self.ak = a0
        # else:
        #     M0 = model.M(t0, model.q0).tocsr()
        #     rhs0 = self.model.h(t0, model.q0, model.u0) + self.model.W_g(t0, model.q0) @ model.la_g0 + self.model.W_gamma(t0, model.q0) @ model.la_gamma0
        #     self.ak = spsolve(M0, rhs0, use_umfpack=False)
        # self.a_bark = self.ak.copy()
        self.q_bark = self.qk.copy()

        if numerical_jacobian:
            self.__R_gen = self.__R_gen_num
        else:
            self.__R_gen = self.__R_gen_analytic

    def update(self, qk1, store=False):
        # """update dependent variables modifed version of Capobianco2019 (17):
        # - q_dot(uk) instead of uk
        # - q_ddot(a_beta) instead of a_beta (weighted a_beta is used inside q_ddot instead of evaluating it twice with both parts)
        # - B @ Qk1 instead of Qk1
        # """
        """
        Generaliezd alpha discrete equation, see Botasso2008 (12):  \\
        M @ ak1 = hk1 + W_gk1 @ la_gk1 (12a)\\
        uk1 = uk + dt * ((1 - gamma) * ak + gamma * ak1) (12b) \\
        qk1 = qk + dt * uk + 0.5 * dt^2 * ((1 - beta) * ak + 2 * beta * ak1) (12c) \\
        gk1 = 0 (12d)
        """
        dt = self.dt
        dt2 = dt * dt

        # from Bottasso2008 (12c)
        # TODO: Check that!
        # TODO: How do we deal with systems with kinematic differential
        #       equation? Maybe we have to implement the generalized alpha
        #       in first order form as well?
        ak1 = (qk1 - self.qk - dt * self.uk) / (self.beta * dt2) - 0.5 * (
            1 - self.beta
        ) * self.ak

        # Bottasso2008 (12b)
        uk1 = self.uk + dt * ((1 - self.gamma) * self.ak + self.gamma * ak1)

        return uk1, ak1

        # a_bark1 = (self.alpha_f * self.ak + (1 - self.alpha_f) * ak1 - self.alpha_m * self.a_bark) / (1 - self.alpha_m)
        # uk1 = self.uk + dt * ((1 - self.gamma) * self.a_bark + self.gamma * a_bark1)
        # a_beta = (0.5 - self.beta) * self.a_bark + self.beta * a_bark1
        # if store:
        #     self.a_bark = a_bark1
        # qk1 = self.qk + dt * self.model.q_dot(self.tk, self.qk, self.uk) + dt2 * self.model.q_ddot(self.tk, self.qk, self.uk, a_beta)
        # return qk1, uk1

    def pack(self, q, la_g, la_gamma):
        nq = self.nq
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        x = np.zeros(self.nR)

        x[:nq] = q
        x[nq : nq + nla_g] = la_g
        x[nq + nla_g : nq + nla_g + nla_gamma] = la_gamma

        return x

    def unpack(self, x):
        nq = self.nq
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        # positions
        q = x[:nq]

        # constraints on position level
        la_g = x[nq : nq + nla_g]

        # constraints on velocity level
        la_gamma = x[nq + nla_g : nq + nla_g + nla_gamma]

        return q, la_g, la_gamma

    def __R_gen_num(self, tk1, xk1):
        yield self.__R(tk1, xk1)
        yield csr_matrix(self.__R_x_num(tk1, xk1))

    def __R_gen_analytic(self, tk1, xk1):
        nq = self.nq
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        # unpack x and update kinematic variables update dependent variables
        qk1, la_gk1, la_gammak1 = self.unpack(xk1)
        uk1, ak1 = self.update(qk1)

        # evaluate mass matrix, constraint force directions and rhs
        Mk1 = self.model.M(tk1, qk1)
        W_gk1 = self.model.W_g(tk1, qk1)
        W_gammak1 = self.model.W_gamma(tk1, qk1)

        hk1 = self.model.h(tk1, qk1, uk1)

        # auxiliary variables
        dt = self.dt
        dt2 = dt * dt

        ###################
        # evaluate residual
        ###################
        R = np.zeros(self.nR)

        # equations of motion, see Botasso2008 (27a)
        # TODO: How to deal with first order form?
        jn = Mk1 @ (
            self.qk / (self.beta * dt2)
            + self.uk / (self.beta * dt)
            - (1 - 1 / (2 * self.beta)) * self.ak
        )
        R[:nq] = (
            Mk1 @ uk1 / (self.beta * dt2)
            - (+hk1 + W_gk1 @ la_gk1 + W_gammak1 @ la_gammak1)
            - jn
        )

        # constraints on position level
        R[nq : nq + nla_g] = self.model.g(tk1, qk1)

        # constraints on velocity level
        R[nq + nla_g : nq + nla_g + nla_gamma] = self.model.gamma(tk1, qk1, uk1)

        yield R

        raise NotImplementedError("")

        ###############################################################################################
        # R[:nu] = Mk1 @ ak1 -( self.model.h(tk1, qk1, uk1) + W_gk1 @ la_gk1 + W_gammak1 @ la_gammak1 )
        ###############################################################################################
        Wla_g_q = self.model.Wla_g_q(tk1, qk1, la_gk1, scipy_matrix=csr_matrix)
        Wla_gamma_q = self.model.Wla_gamma_q(
            tk1, qk1, la_gammak1, scipy_matrix=csr_matrix
        )
        rhs_q = -(self.model.h_q(tk1, qk1, uk1) + Wla_g_q + Wla_gamma_q)
        rhs_u = -self.model.h_u(tk1, qk1, uk1)
        rhs_a = rhs_q @ self.q_a + rhs_u * self.u_a
        Ma_a = (
            self.model.Mu_q(tk1, qk1, ak1, scipy_matrix=csr_matrix) @ self.q_a + rhs_a
        )

        Ra_a = Mk1 + Ma_a
        Ra_la_g = -W_gk1
        Ra_la_g *= self.pre_cond
        Ra_la_gamma = -W_gammak1

        #########################################
        # R[nu:nu+nla_g] = self.model.g(tk1, qk1)
        #########################################
        Rla_g_a = self.model.g_q(tk1, qk1) @ self.q_a
        Rla_g_la_g = None
        Rla_g_la_gamma = None

        ##################################################################
        # R[nu+nla_g:nu+nla_g+nla_gamma] = self.model.gamma(tk1, qk1, uk1)
        ##################################################################
        Rla_gamma_a = (
            self.model.gamma_q(tk1, qk1, uk1) @ self.q_a
            + self.model.gamma_u(tk1, qk1) * self.u_a
        )
        Rla_gamma_la_g = None
        Rla_gamma_la_gamma = None

        # sparse assemble global tangent matrix
        R_x = bmat(
            [
                [Ra_a, Ra_la_g, Ra_la_gamma],
                [Rla_g_a, Rla_g_la_g, Rla_g_la_gamma],
                [Rla_gamma_a, Rla_gamma_la_g, Rla_gamma_la_gamma],
            ],
            format="csr",
        )

        yield R_x

        # R_x_num = self.__R_x_num(tk1, xk1)
        # diff = R_x.toarray() - R_x_num

        # # diff_error = diff[:nu] #~1.0e-5

        # # diff_error = diff[nu+nla_g:nu+nla_g+nla_gamma]

        # diff_error = diff #~1.0e-5

        # error = np.max(np.abs(diff_error))
        # print(f'absolute error R_x = {error}')

        # # error = np.max(np.abs(diff_error)) / np.max(np.abs(R_x_num))
        # # print(f'relative error R_x = {error}')

        # yield R_x_num

    def __R(self, tk1, xk1):
        return next(self.__R_gen_analytic(tk1, xk1))

    def __R_x_num(self, tk1, xk1):
        return csr_matrix(
            approx_fprime(xk1, lambda x: self.__R(tk1, x), method="2-point")
        )

    def step(self, tk1, xk1):
        # initial residual and error
        R_gen = self.__R_gen(tk1, xk1)
        R = next(R_gen)
        error = self.error_function(R)
        converged = error < self.tol
        j = 0
        if not converged:
            while j < self.max_iter:
                # jacobian
                R_x = next(R_gen)

                # Newton update
                j += 1
                dx = spsolve(R_x, R, use_umfpack=True)
                # TODO:
                dx[self.nu :] *= self.pre_cond
                xk1 -= dx
                R_gen = self.__R_gen(tk1, xk1)
                R = next(R_gen)

                error = self.error_function(R)
                converged = error < self.tol
                if converged:
                    break

        return converged, j, error, xk1

    def solve(self):
        dt = self.dt
        dt2 = self.dt**2

        # lists storing output variables
        t = [self.tk]
        q = [self.qk]
        u = [self.uk]
        # a = [self.ak]
        la_g = [self.la_gk]
        la_gamma = [self.la_gammak]

        pbar = tqdm(np.arange(self.t0, self.t1, self.dt))
        for _ in pbar:
            # evaluate quantities at previous time step
            self.q_a = (
                dt2
                * self.beta
                * self.alpha_ratio
                * self.model.B(self.tk, self.qk, scipy_matrix=csr_matrix)
            )
            self.u_a = dt * self.gamma * self.alpha_ratio

            # initial guess for Newton-Raphson solver and time step
            tk1 = self.tk + self.dt
            # xk1 = self.pack(self.ak, self.la_gk, self.la_gammak)
            xk1 = self.pack(self.qk, self.la_gk, self.la_gammak)
            converged, n_iter, error, xk1 = self.step(tk1, xk1)
            qk1, la_gk1, la_gammak1 = self.unpack(xk1)

            # update progress bar and check convergence
            pbar.set_description(
                f"t: {tk1:0.2e}s < {self.t1:0.2e}s; Newton: {n_iter}/{self.max_iter} iterations; error: {error:0.2e}"
            )
            if not converged:
                raise RuntimeError(
                    f"internal Newton-Raphson method not converged after {n_iter} steps with error: {error:.5e}"
                )

            # update dependent variables
            qk1, uk1 = self.update(ak1, store=True)

            # modify converged quantities
            qk1, uk1 = self.model.step_callback(tk1, qk1, uk1)

            # store soltuion fields
            t.append(tk1)
            q.append(qk1)
            u.append(uk1)
            a.append(ak1)
            la_gamma.append(la_gammak1)

            # update local variables for accepted time step
            self.tk = tk1
            self.qk = qk1
            self.uk = uk1
            self.ak = ak1
            self.la_gk = la_gk1
            self.la_gammak = la_gammak1

        # write solution
        return Solution(
            t=np.array(t),
            q=np.array(q),
            u=np.array(u),
            a=np.array(a),
            la_g=np.array(la_g),
            la_gamma=np.array(la_gamma),
        )


class GenAlphaDAEAcc:
    """Generalized alpha solver for constraint mechanical systems of DAE 
    index 3 with the right precondtioner introduced in Arnold2008 and 
    Bottasso2008. The (a, lambda) form is implemented here. Aditionally 
    constraints on velocity level can be solved.

    To-Do:
    -----
    Study Bottasso2008 and figure out if we have to use another preconditioner 
    for the constraints on velocity level?

    References
    ----------
    Arnold2008: https://doi.org/10.1007/s11044-007-9084-0 \\
    Bottasso2008 https://doi.org/10.1007/s11044-007-9051-9
    """

    def __init__(
        self,
        model,
        t1,
        dt,
        rho_inf=1,
        beta=None,
        gamma=None,
        alpha_m=None,
        alpha_f=None,
        newton_tol=1e-8,
        newton_max_iter=40,
        newton_error_function=lambda x: np.max(np.abs(x)),
        numerical_jacobian=False,
        a0=None,
        pre_cond=False,
    ):

        self.model = model

        # integration time
        self.t0 = t0 = model.t0
        self.t1 = (
            t1 if t1 > t0 else ValueError("t1 must be larger than initial time t0.")
        )
        self.dt = dt

        # parameter
        self.rho_inf = rho_inf
        if None in [beta, gamma, alpha_m, alpha_f]:
            self.alpha_m = (2 * rho_inf - 1) / (1 + rho_inf)
            self.alpha_f = rho_inf / (1 + rho_inf)
            self.gamma = 0.5 + self.alpha_f - self.alpha_m
            self.beta = 0.25 * ((self.gamma + 0.5) ** 2)
        else:
            self.gamma = gamma
            self.beta = beta
            self.alpha_m = alpha_m
            self.alpha_f = alpha_f
        self.alpha_ratio = (1 - self.alpha_f) / (1 - self.alpha_m)

        # usage of a right preconditioner, see Arnold2008 and Bottasso2008
        if pre_cond:
            self.pre_cond = 1.0 / (self.dt**2 * self.beta)
        else:
            self.pre_cond = 1  # no preconditioning

        # newton settings
        self.newton_tol = newton_tol
        self.newton_max_iter = newton_max_iter
        self.newton_error_function = newton_error_function

        # dimensions
        self.nq = model.nq
        self.nu = model.nu
        self.nla_g = model.nla_g
        self.nla_gamma = model.nla_gamma

        # equation sof motion, constraints on position level and constraints on velocitiy level
        self.nR = self.nu + self.nla_g + self.nla_gamma

        self.tk = model.t0
        self.qk = model.q0
        self.uk = model.u0
        self.la_gk = model.la_g0
        self.la_gammak = model.la_gamma0

        if a0 is not None:
            self.ak = a0
        else:
            M0 = model.M(t0, model.q0).tocsr()
            rhs0 = (
                self.model.h(t0, model.q0, model.u0)
                + self.model.W_g(t0, model.q0) @ model.la_g0
                + self.model.W_gamma(t0, model.q0) @ model.la_gamma0
            )
            self.ak = spsolve(M0, rhs0, use_umfpack=False)
        self.a_bark = self.ak.copy()

        if numerical_jacobian:
            self.__R_gen = self.__R_gen_num
        else:
            self.__R_gen = self.__R_gen_analytic

    def update(self, ak1, store=False):
        """update dependent variables modifed version of Capobianco2019 (17):
        - q_dot(uk) instead of uk
        - q_ddot(a_beta) instead of a_beta (weighted a_beta is used inside q_ddot instead of evaluating it twice with both parts)
        - B @ Qk1 instead of Qk1
        """
        dt = self.dt
        dt2 = dt * dt
        a_bark1 = (
            self.alpha_f * self.ak
            + (1 - self.alpha_f) * ak1
            - self.alpha_m * self.a_bark
        ) / (1 - self.alpha_m)
        uk1 = self.uk + dt * ((1 - self.gamma) * self.a_bark + self.gamma * a_bark1)
        a_beta = (0.5 - self.beta) * self.a_bark + self.beta * a_bark1
        if store:
            self.a_bark = a_bark1
        qk1 = (
            self.qk
            + dt * self.model.q_dot(self.tk, self.qk, self.uk)
            + dt2 * self.model.q_ddot(self.tk, self.qk, self.uk, a_beta)
        )
        return qk1, uk1

    def pack(self, a, la_g, la_gamma):
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        x = np.zeros(self.nR)

        x[:nu] = a
        x[nu : nu + nla_g] = la_g
        x[nu + nla_g : nu + nla_g + nla_gamma] = la_gamma

        return x

    def unpack(self, x):
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        # acceleration
        a = x[:nu]

        # constraints on position level
        la_g = x[nu : nu + nla_g]

        # constraints on velocity level
        la_gamma = x[nu + nla_g : nu + nla_g + nla_gamma]

        return a, la_g, la_gamma

    def __R_gen_num(self, tk1, xk1):
        yield self.__R(tk1, xk1)
        yield csr_matrix(self.__R_x_num(tk1, xk1))

    def __R_gen_analytic(self, tk1, xk1):
        nu = self.nu
        nla_g = self.nla_g
        nla_gamma = self.nla_gamma

        # unpack x and update kinematic variables update dependent variables
        ak1, la_gk1, la_gammak1 = self.unpack(xk1)
        qk1, uk1 = self.update(ak1)

        # evaluate mass matrix and constraint force directions and rhs
        Mk1 = self.model.M(tk1, qk1)
        W_gk1 = self.model.W_g(tk1, qk1)
        W_gammak1 = self.model.W_gamma(tk1, qk1)

        ###################
        # evaluate residual
        ###################
        R = np.zeros(self.nR)

        # equations of motion
        R[:nu] = Mk1 @ ak1 - (
            self.model.h(tk1, qk1, uk1) + W_gk1 @ la_gk1 + W_gammak1 @ la_gammak1
        )

        # constraints on position level
        R[nu : nu + nla_g] = self.model.g(tk1, qk1)

        # constraints on velocity level
        R[nu + nla_g : nu + nla_g + nla_gamma] = self.model.gamma(tk1, qk1, uk1)

        yield R

        ###############################################################################################
        # R[:nu] = Mk1 @ ak1 -( self.model.h(tk1, qk1, uk1) + W_gk1 @ la_gk1 + W_gammak1 @ la_gammak1 )
        ###############################################################################################
        Wla_g_q = self.model.Wla_g_q(tk1, qk1, la_gk1, scipy_matrix=csr_matrix)
        Wla_gamma_q = self.model.Wla_gamma_q(
            tk1, qk1, la_gammak1, scipy_matrix=csr_matrix
        )
        rhs_q = -(self.model.h_q(tk1, qk1, uk1) + Wla_g_q + Wla_gamma_q)
        rhs_u = -self.model.h_u(tk1, qk1, uk1)
        rhs_a = rhs_q @ self.q_a + rhs_u * self.u_a
        Ma_a = (
            self.model.Mu_q(tk1, qk1, ak1, scipy_matrix=csr_matrix) @ self.q_a + rhs_a
        )

        Ra_a = Mk1 + Ma_a
        Ra_la_g = -W_gk1
        Ra_la_g *= self.pre_cond
        Ra_la_gamma = -W_gammak1

        #########################################
        # R[nu:nu+nla_g] = self.model.g(tk1, qk1)
        #########################################
        Rla_g_a = self.model.g_q(tk1, qk1) @ self.q_a
        Rla_g_la_g = None
        Rla_g_la_gamma = None

        ##################################################################
        # R[nu+nla_g:nu+nla_g+nla_gamma] = self.model.gamma(tk1, qk1, uk1)
        ##################################################################
        Rla_gamma_a = (
            self.model.gamma_q(tk1, qk1, uk1) @ self.q_a
            + self.model.gamma_u(tk1, qk1) * self.u_a
        )
        Rla_gamma_la_g = None
        Rla_gamma_la_gamma = None

        # sparse assemble global tangent matrix
        R_x = bmat(
            [
                [Ra_a, Ra_la_g, Ra_la_gamma],
                [Rla_g_a, Rla_g_la_g, Rla_g_la_gamma],
                [Rla_gamma_a, Rla_gamma_la_g, Rla_gamma_la_gamma],
            ],
            format="csr",
        )

        yield R_x

        # R_x_num = self.__R_x_num(tk1, xk1)
        # diff = R_x.toarray() - R_x_num

        # # diff_error = diff[:nu] #~1.0e-5

        # # diff_error = diff[nu+nla_g:nu+nla_g+nla_gamma]

        # diff_error = diff #~1.0e-5

        # error = np.max(np.abs(diff_error))
        # print(f'absolute error R_x = {error}')

        # # error = np.max(np.abs(diff_error)) / np.max(np.abs(R_x_num))
        # # print(f'relative error R_x = {error}')

        # yield R_x_num

    def __R(self, tk1, xk1):
        return next(self.__R_gen_analytic(tk1, xk1))

    def __R_x_num(self, tk1, xk1):
        return csr_matrix(
            approx_fprime(xk1, lambda x: self.__R(tk1, x), method="2-point")
        )

    def step(self, tk1, xk1):
        # initial residual and error
        R_gen = self.__R_gen(tk1, xk1)
        R = next(R_gen)
        error = self.newton_error_function(R)
        converged = error < self.newton_tol
        j = 0
        if not converged:
            while j < self.newton_max_iter:
                # jacobian
                R_x = next(R_gen)

                # Newton update
                j += 1
                dx = spsolve(R_x, R, use_umfpack=True)
                dx[self.nu :] *= self.pre_cond
                xk1 -= dx
                R_gen = self.__R_gen(tk1, xk1)
                R = next(R_gen)

                error = self.newton_error_function(R)
                converged = error < self.newton_tol
                if converged:
                    break

        return converged, j, error, xk1

    def solve(self):
        dt = self.dt
        dt2 = self.dt**2

        # lists storing output variables
        t = [self.tk]
        q = [self.qk]
        u = [self.uk]
        a = [self.ak]
        la_g = [self.la_gk]
        la_gamma = [self.la_gammak]

        pbar = tqdm(np.arange(self.t0, self.t1, self.dt))
        for _ in pbar:
            # evaluate quantities at previous time step
            self.q_a = (
                dt2
                * self.beta
                * self.alpha_ratio
                * self.model.B(self.tk, self.qk, scipy_matrix=csr_matrix)
            )
            self.u_a = dt * self.gamma * self.alpha_ratio

            # initial guess for Newton-Raphson solver and time step
            tk1 = self.tk + self.dt
            xk1 = self.pack(self.ak, self.la_gk, self.la_gammak)
            converged, n_iter, error, xk1 = self.step(tk1, xk1)
            ak1, la_gk1, la_gammak1 = self.unpack(xk1)

            # update progress bar and check convergence
            pbar.set_description(
                f"t: {tk1:0.2e}s < {self.t1:0.2e}s; Newton: {n_iter}/{self.newton_max_iter} iterations; error: {error:0.2e}"
            )
            if not converged:
                raise RuntimeError(
                    f"internal Newton-Raphson method not converged after {n_iter} steps with error: {error:.5e}"
                )

            # update dependent variables
            qk1, uk1 = self.update(ak1, store=True)

            # modify converged quantities
            qk1, uk1 = self.model.step_callback(tk1, qk1, uk1)

            # store soltuion fields
            t.append(tk1)
            q.append(qk1)
            u.append(uk1)
            a.append(ak1)
            la_g.append(la_gk1)
            la_gamma.append(la_gammak1)

            # update local variables for accepted time step
            self.tk = tk1
            self.qk = qk1
            self.uk = uk1
            self.ak = ak1
            self.la_gk = la_gk1
            self.la_gammak = la_gammak1

        # write solution
        return Solution(
            t=np.array(t),
            q=np.array(q),
            u=np.array(u),
            a=np.array(a),
            la_g=np.array(la_g),
            la_gamma=np.array(la_gamma),
        )
