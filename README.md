This repository provides the computational code accompanying the following paper:

A Menu-based Strategy for Real-time Scheduling of Electric Vehicle Charging Requests on Expressways


1. Overview
   
This paper develops a menu-based strategy for real-time scheduling of electric vehicle (EV) charging requests on expressways. The proposed approach coordinates charging requests across multiple charging stations by accounting for uncertain request arrivals and charging queues under the first-come, first-served (FCFS) rule.

The repository provides implementations of the stochastic programming model, the customized branch-and-cut algorithm, the rolling horizon optimization (RHO) framework, and the full-information model solved by column generation.

2. Code Structure

The repository contains four main source files:

| waiting_time.c | C implementation for computing conditional expected waiting times. |

| `menu_sp.py` | Single-period stochastic programming model [ReSP], solved using a customized branch-and-cut algorithm. |

| `RHO.py` | Rolling horizon optimization framework that sequentially solves the single-period model for each time slot and updates charging station states. |

| `CG.py` | Column generation algorithm for solving the linear relaxation of the full-information model [DP], providing a lower bound on the realized total waiting time. |

3. Computational Environment

The numerical experiments reported in the paper were conducted using:

- Python 3.11.4
- Gurobi 11.0.1
- 64-bit Windows
- 16 GB RAM
- Intel Core i9-13900HX CPU at 2.20 GHz

A valid Gurobi license and a C compiler are required to run the optimization models.

4. Data and Experimental Settings

The experimental instances are constructed according to the procedures described in Section 6 and the supplementary material of the paper.

Three experimental settings are considered:

- Small-scale instances with 90 potential charging requests.
- Expressway instances with 8,640 potential charging requests.
- Connected expressway network instances with 20,448 potential charging requests.

The network configurations, charging station locations, request characteristics, and experimental assumptions are detailed in the paper.

The temporal distribution of charging demand is calibrated using publicly available charging data from Chen et al. (2024). The associated data and code repository is available at:

https://github.com/chenli-ora/EV-OR2023

5. Algorithms

Branch-and-Cut Algorithm: The customized branch-and-cut algorithm solves the single-period stochastic programming model [ReSP] by dynamically generating allocation-based optimality cuts.

Rolling Horizon Optimization: The RHO framework applies the single-period model sequentially over the planning horizon. In each time slot, it generates an allocation menu, implements the recommendations for realized requests, and updates charging station states for the next time slot.

Full-Information Column Generation: The column generation algorithm solves the linear relaxation of the full-information model [DP]. A tailored label-setting algorithm is used to solve the station-specific pricing subproblems to optimality. The resulting objective value provides a lower bound on the realized total waiting time.

6. Reference

Please refer to the paper and its supplementary material for the complete mathematical formulations, algorithmic details, experimental settings, and numerical results.
