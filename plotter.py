
import matplotlib
# Use Agg backend to avoid GUI requirement for matplotlib, since we just want images
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import io
import wx
import numpy as np

class Plotter:
    def __init__(self, debug=False):
        self.debug = debug

    @staticmethod
    def _fit_xy(axis, bounds, invert_y=False):
        if not bounds:
            axis.set_aspect('equal', adjustable='box')
            return
        min_x, min_y, max_x, max_y = bounds
        pad = max(1.0, 0.025 * max(max_x - min_x, max_y - min_y))
        axis.set_xlim(min_x - pad, max_x + pad)
        axis.set_ylim((-max_y - pad, -min_y + pad) if invert_y else (min_y - pad, max_y + pad))

    @staticmethod
    def _figsize(bounds, base=8.5):
        if not bounds:
            return (base, 6.5)
        width, height = max(bounds[2] - bounds[0], 1.0), max(bounds[3] - bounds[1], 1.0)
        return (base, max(4.8, min(8.0, base * height / width)))

    def plot_3d_mesh(self, mesh, stackup=None, vmin=None, vmax=None, board_bounds=None):
        """
        Generates a 3D scatter plot of the mesh nodes.
        Returns a wx.Bitmap.
        """
        try:
            fig = plt.figure(figsize=self._figsize(board_bounds), constrained_layout=True)
            ax = fig.add_subplot(111, projection='3d')
            
            xs, ys, zs, c = [], [], [], []
            has_results = hasattr(mesh, 'results') and mesh.results
            
            layer_to_z = {}
            if stackup and 'layer_order' in stackup:
                for idx, layer_id in enumerate(stackup['layer_order']):
                    layer_to_z[layer_id] = 10.0 - idx
            else:
                copper_layers = sorted(stackup['copper'].keys()) if stackup and 'copper' in stackup else []
                for idx, layer_id in enumerate(copper_layers):
                    layer_to_z[layer_id] = 10.0 - idx
            
            # If no node_coords populated yet (edge case), return None
            if not mesh.node_coords:
                plt.close(fig)
                return None

            for nid, (x, y, layer) in mesh.node_coords.items():
                if has_results and nid not in mesh.results:
                    continue
                xs.append(x)
                ys.append(-y)  # Invert Y to match KiCad
                zs.append(layer_to_z.get(layer, 10 - layer * 0.5))
                c.append(mesh.results.get(nid, 0.0) if has_results else layer)

            if not xs:
                plt.close(fig)
                return None
                
            sc = ax.scatter(xs, ys, zs, c=c, cmap='viridis', vmin=vmin, vmax=vmax)
            if has_results:
                plt.colorbar(sc, label='Voltage (V)', shrink=0.8)
            
            ax.set_xlabel('X (mm)'); ax.set_ylabel('Y (mm)'); ax.set_zlabel('L (pseudo)')
            
            # Equal aspect ratio
            x_limits = (board_bounds[0], board_bounds[2]) if board_bounds else ax.get_xlim3d()
            y_limits = (-board_bounds[3], -board_bounds[1]) if board_bounds else ax.get_ylim3d()
            x_range = x_limits[1] - x_limits[0]
            y_range = y_limits[1] - y_limits[0]
            max_range = max(x_range, y_range)
            x_mid = (x_limits[0] + x_limits[1]) / 2.0
            y_mid = (y_limits[0] + y_limits[1]) / 2.0
            ax.set_xlim3d([x_mid - max_range/2, x_mid + max_range/2])
            ax.set_ylim3d([y_mid - max_range/2, y_mid + max_range/2])
            
            return self._fig_to_bitmap(fig)
        except Exception as e:
            if self.debug: print(f"Plotter 3D Error: {e}")
            return None

    def plot_layer_2d(self, mesh, layer_id, stackup=None, vmin=None, vmax=None, layer_name=None, board_bounds=None):
        """
        Generates a 2D plot (heatmap) for a specific layer.
        Returns a wx.Bitmap.
        """
        try:
            fig, ax = plt.subplots(figsize=self._figsize(board_bounds), constrained_layout=True)
            
            xs, ys, vs = [], [], []
            has_results = hasattr(mesh, 'results') and mesh.results
            
            # Filter nodes for this layer
            nodes_on_layer = [
                nid for nid in mesh.nodes
                if mesh.node_coords[nid][2] == layer_id
                and (not has_results or nid in mesh.results)
            ]
            
            if not nodes_on_layer:
                plt.close(fig)
                return None

            for nid in nodes_on_layer:
                coords = mesh.node_coords[nid]
                xs.append(coords[0])
                ys.append(-coords[1]) # Invert Y
                val = mesh.results.get(nid, 0.0) if has_results else 0.0
                vs.append(val)
                
            if not xs:
                plt.close(fig)
                return None

            # Scatter plot for now - tripcolor or imshow is better if we have regular grid, 
            # but scatter is robust for sparse nodes.
            # Using a fixed marker size might be tricky, let's try a reasonable default.
            # Ideally s should relate to grid_size, but scatter size is in points^2.
            # Let's just use a standard size for visibility.
            sc = ax.scatter(xs, ys, c=vs, cmap='viridis', vmin=vmin, vmax=vmax, s=20)
            
            if has_results:
                plt.colorbar(sc, label='Voltage (V)')
            
            if layer_name is None:
                layer_name = str(layer_id)
                if stackup and 'copper' in stackup and layer_id in stackup['copper']:
                     # Try to get layer name? currently stackup dict structure in test is simple
                     pass

            ax.set_title(f"Layer: {layer_name}")
            ax.set_xlabel('X (mm)')
            ax.set_ylabel('Y (mm)')
            ax.set_aspect('equal', 'box')
            self._fit_xy(ax, board_bounds, invert_y=True)
            
            return self._fig_to_bitmap(fig)

        except Exception as e:
            if self.debug: print(f"Plotter 2D Error: {e}")
            return None

    def plot_impedance_sweep(self, baseline, optimized=None):
        """Plot rail impedance magnitude and phase over frequency."""
        try:
            fig, (ax_mag, ax_phase) = plt.subplots(2, 1, figsize=(8, 6), sharex=True, constrained_layout=True)
            frequencies = np.asarray(baseline.frequencies_hz)
            impedance = np.asarray(baseline.impedance_ohm, dtype=complex)
            ax_mag.loglog(frequencies, np.abs(impedance), label="Baseline", linewidth=2)
            ax_phase.semilogx(frequencies, np.angle(impedance, deg=True), label="Baseline", linewidth=2)

            if optimized is not None:
                optimized_impedance = np.asarray(optimized.impedance_ohm, dtype=complex)
                ax_mag.loglog(frequencies, np.abs(optimized_impedance), label="Optimized", linewidth=2)
                ax_phase.semilogx(frequencies, np.angle(optimized_impedance, deg=True), label="Optimized", linewidth=2)

            target = baseline.target_impedance_ohm
            if target > 0:
                ax_mag.axhline(target, color="red", linestyle="--", label=f"Target ({target:g} ohm)")

            ax_mag.set_ylabel("|Z| (ohm)")
            ax_mag.set_title("Rail-to-ground impedance")
            ax_mag.grid(True, which="both", alpha=0.3)
            ax_mag.legend()
            ax_phase.set_xlabel("Frequency (Hz)")
            ax_phase.set_ylabel("Phase (deg)")
            ax_phase.grid(True, which="both", alpha=0.3)
            return self._fig_to_bitmap(fig)
        except Exception as e:
            if self.debug:
                print(f"Impedance plot error: {e}")
            return None

    def plot_differential_impedance(self, results, as_png=False):
        """Plot length-weighted Zdiff with per-section min/max ranges."""
        try:
            plotted = [result for result in results if result.weighted_impedance_ohm > 0]
            if not plotted:
                return None
            labels = [result.pair.name for result in plotted]
            values = np.asarray([result.weighted_impedance_ohm for result in plotted])
            lower = values - np.asarray([result.minimum_impedance_ohm for result in plotted])
            upper = np.asarray([result.maximum_impedance_ohm for result in plotted]) - values
            colors = [
                "#2ca02c" if result.status == "PASS" else
                "#d62728" if result.status == "FAIL" else "#ffbf00"
                for result in plotted
            ]
            fig, axis = plt.subplots(figsize=(8, 5), constrained_layout=True)
            x = np.arange(len(plotted))
            axis.bar(x, values, color=colors, alpha=0.85)
            axis.errorbar(x, values, yerr=np.vstack((lower, upper)), fmt="none", color="black", capsize=4)
            for index, result in enumerate(plotted):
                axis.plot(index, result.pair.target_impedance_ohm, marker="D", color="blue")
            axis.set_xticks(x, labels, rotation=25, ha="right")
            axis.set_ylabel("Differential impedance (ohm)")
            axis.set_title("Stackup-aware differential impedance")
            axis.grid(True, axis="y", alpha=0.3)
            axis.scatter([], [], marker="D", color="blue", label="Target")
            axis.legend()
            return self._fig_to_png(fig) if as_png else self._fig_to_bitmap(fig)
        except Exception as e:
            if self.debug:
                print(f"Differential impedance plot error: {e}")
            return None

    def plot_stackup_profile(self, stackup, as_png=False):
        """Render an ordered physical stackup cross-section."""
        try:
            if stackup is None or not stackup.layers:
                return None
            total = sum(max(layer.thickness_mm, 1e-6) for layer in stackup.layers)
            fig, axis = plt.subplots(figsize=(8, 5), constrained_layout=True)
            y = 0.0
            for layer in reversed(stackup.layers):
                height = max(layer.thickness_mm, total * 0.012 if layer.kind == "COPPER" else 1e-6)
                color = "#d98c10" if layer.kind == "COPPER" else "#6bbf59"
                axis.barh(0, height, left=y, height=0.55, color=color, edgecolor="black")
                label = f"{layer.name}: {layer.thickness_mm:g} mm"
                if layer.kind != "COPPER":
                    label += f", Er={layer.epsilon_r:g}"
                axis.text(y + height / 2, 0, label, rotation=90, va="center", ha="center", fontsize=8)
                y += height
            axis.set_xlim(0, max(y, 1e-6))
            axis.set_ylim(-0.55, 0.55)
            axis.set_yticks([])
            axis.set_xlabel("Physical thickness from bottom to top (mm)")
            axis.set_title(f"PCB stackup — {stackup.source}")
            return self._fig_to_png(fig) if as_png else self._fig_to_bitmap(fig)
        except Exception as e:
            if self.debug:
                print(f"Stackup plot error: {e}")
            return None

    def plot_thermal_3d(self, mesh, result, as_png=False, board_bounds=None):
        """Render the solved volumetric temperature field."""
        try:
            nodes = list(mesh.nodes)
            if len(nodes) > 50000:
                stride = int(np.ceil(len(nodes) / 50000.0))
                nodes = nodes[::stride]
            coords = np.asarray([mesh.node_coords[node] for node in nodes], dtype=float)
            temperatures = np.asarray([result.temperatures_c[node] for node in nodes], dtype=float)
            bounds = board_bounds or getattr(mesh, 'bounds_mm', None)
            fig = plt.figure(figsize=self._figsize(bounds), constrained_layout=True)
            axis = fig.add_subplot(111, projection='3d')
            # KiCad's board-space Y axis grows downwards.  Matplotlib's view
            # grows upwards, so negate Y to retain the PCB's screen/cardinal
            # orientation: KiCad's upper-right stays upper-right in 3D.
            display_y = -coords[:, 1]
            scatter = axis.scatter(
                coords[:, 0], display_y, coords[:, 2], c=temperatures,
                cmap='inferno', s=5, alpha=0.8,
            )
            axis.set_xlabel('X (mm)')
            axis.set_ylabel('Y (mm)')
            axis.set_zlabel('Z (mm)')
            axis.set_title('3D board temperature')
            if bounds:
                min_x, min_y, max_x, max_y = bounds
                pad = max(1.0, 0.025 * max(max_x - min_x, max_y - min_y))
                axis.set_xlim(min_x - pad, max_x + pad)
                axis.set_ylim(-max_y - pad, -min_y + pad)
                z_span = max(float(np.ptp(coords[:, 2])), 0.02 * max(max_x - min_x, max_y - min_y))
                axis.set_box_aspect((max_x - min_x + 2 * pad, max_y - min_y + 2 * pad, z_span))
            fig.colorbar(scatter, ax=axis, label='Temperature (C)', shrink=0.75)
            return self._fig_to_png(fig) if as_png else self._fig_to_bitmap(fig)
        except Exception as e:
            if self.debug:
                print(f"Thermal 3D plot error: {e}")
            return None

    def plot_thermal_surface(self, mesh, result, side='TOP', as_png=False, board_bounds=None):
        """Render a top or bottom board temperature map."""
        try:
            if not mesh.node_map:
                return None
            # Thermal stackup order follows KiCad's F.Cu -> B.Cu order.
            target_iz = 0 if side.upper() == 'TOP' else max(key[2] for key in mesh.node_map)
            surface = self._thermal_surface_grid(mesh, result, target_iz)
            if surface is None:
                return None
            x_edges, y_edges, temperatures = surface
            bounds = board_bounds or getattr(mesh, 'bounds_mm', None)
            fig, axis = plt.subplots(figsize=self._figsize(bounds), constrained_layout=True)
            # A scatter plot of square markers leaves pixel-sized gaps between
            # cells, which looks like a black grid at normal GUI zoom.  The
            # thermal mesh is already a regular finite-volume grid: render
            # its cells directly, without visible marker borders.
            plot = axis.pcolormesh(
                x_edges, y_edges, temperatures,
                cmap='inferno', shading='flat', edgecolors='none',
                linewidth=0.0, antialiased=False, rasterized=True,
            )
            self._fit_xy(axis, bounds)
            # KiCad board coordinates grow downwards on screen.  Keep the
            # field in native board coordinates (so cells retain their exact
            # locations), then invert only the display axis: the PCB's upper
            # right corner stays upper right for both surface views.
            axis.invert_yaxis()
            axis.set_xlabel('X (mm)')
            axis.set_ylabel('Y (mm)')
            axis.set_title(f'{side.title()} surface temperature')
            fig.colorbar(plot, ax=axis, label='Temperature (C)')
            return self._fig_to_png(fig) if as_png else self._fig_to_bitmap(fig)
        except Exception as e:
            if self.debug:
                print(f"Thermal surface plot error: {e}")
            return None

    @staticmethod
    def _thermal_surface_grid(mesh, result, target_iz):
        """Return cell edges and a temperature field for one mesh surface."""
        cells = [
            (ix, iy, node)
            for (ix, iy, iz), node in mesh.node_map.items()
            if iz == target_iz and node in result.temperatures_c
        ]
        if not cells:
            return None
        x_indices = sorted({ix for ix, _, _ in cells})
        y_indices = sorted({iy for _, iy, _ in cells})
        x_positions = {
            ix: mesh.node_coords[node][0]
            for ix, _, node in cells
        }
        y_positions = {
            iy: mesh.node_coords[node][1]
            for _, iy, node in cells
        }
        x_centers = np.asarray([x_positions[ix] for ix in x_indices], dtype=float)
        y_centers = np.asarray([y_positions[iy] for iy in y_indices], dtype=float)
        temperatures = np.full((len(y_indices), len(x_indices)), np.nan, dtype=float)
        x_offset = {ix: index for index, ix in enumerate(x_indices)}
        y_offset = {iy: index for index, iy in enumerate(y_indices)}
        for ix, iy, node in cells:
            temperatures[y_offset[iy], x_offset[ix]] = result.temperatures_c[node]

        def cell_edges(centers):
            if len(centers) > 1:
                step = float(np.median(np.diff(centers)))
            else:
                step = float(getattr(mesh, 'grid_size_mm', 1.0) or 1.0)
            return np.concatenate((
                [centers[0] - step / 2.0],
                (centers[:-1] + centers[1:]) / 2.0,
                [centers[-1] + step / 2.0],
            ))

        return cell_edges(x_centers), cell_edges(y_centers), temperatures

    def _cfd_field(self, mesh, result, field):
        shape = mesh.shape
        field = field.upper()
        if field == 'PRESSURE':
            return np.asarray(result.pressure_pa, dtype=float).reshape(shape), 'Pressure (Pa)', 'coolwarm'
        if field == 'VELOCITY':
            u = np.asarray(result.velocity_u_m_s, dtype=float).reshape(shape)
            v = np.asarray(result.velocity_v_m_s, dtype=float).reshape(shape)
            w = np.asarray(result.velocity_w_m_s, dtype=float).reshape(shape)
            return np.sqrt(u * u + v * v + w * w), 'Velocity (m/s)', 'viridis'
        values = np.asarray(result.air_temperature_c, dtype=float).reshape(shape)
        solids = np.asarray(result.solid_temperature_c, dtype=float).reshape(shape)
        values = np.where(np.isfinite(solids), solids, values)
        return values, 'Temperature (C)', 'inferno'

    def plot_cfd_slice(self, mesh, result, field='TEMPERATURE', plane='XY'):
        """Render a central scalar slice with in-plane velocity vectors."""
        try:
            values, label, cmap = self._cfd_field(mesh, result, field)
            shape = mesh.shape
            plane = plane.upper()
            u = np.asarray(result.velocity_u_m_s, dtype=float).reshape(shape)
            v = np.asarray(result.velocity_v_m_s, dtype=float).reshape(shape)
            w = np.asarray(result.velocity_w_m_s, dtype=float).reshape(shape)
            if plane == 'XZ':
                index = shape[1] // 2
                image_values = values[:, index, :].T
                vector_a, vector_b = u[:, index, :].T, w[:, index, :].T
                extent = (0, mesh.dimensions_m[0] * 1000, 0, mesh.dimensions_m[2] * 1000)
                axes = ('X (mm)', 'Z (mm)')
            elif plane == 'YZ':
                index = shape[0] // 2
                image_values = values[index, :, :].T
                vector_a, vector_b = v[index, :, :].T, w[index, :, :].T
                extent = (0, mesh.dimensions_m[1] * 1000, 0, mesh.dimensions_m[2] * 1000)
                axes = ('Y (mm)', 'Z (mm)')
            else:
                index = shape[2] // 2
                image_values = values[:, :, index].T
                vector_a, vector_b = u[:, :, index].T, v[:, :, index].T
                extent = (0, mesh.dimensions_m[0] * 1000, 0, mesh.dimensions_m[1] * 1000)
                axes = ('X (mm)', 'Y (mm)')
            fig, axis = plt.subplots(figsize=(8, 5), constrained_layout=True)
            image_plot = axis.imshow(
                np.ma.masked_invalid(image_values), origin='lower', extent=extent,
                aspect='auto', cmap=cmap,
            )
            stride = max(1, int(max(image_values.shape) / 20))
            x = np.linspace(extent[0], extent[1], image_values.shape[1])
            y = np.linspace(extent[2], extent[3], image_values.shape[0])
            axis.quiver(
                x[::stride], y[::stride],
                vector_a[::stride, ::stride], vector_b[::stride, ::stride],
                color='white', alpha=0.65, scale=None,
            )
            axis.set_xlabel(axes[0]); axis.set_ylabel(axes[1])
            axis.set_title(f'Enclosure CFD {field.title()} - {plane} slice')
            fig.colorbar(image_plot, ax=axis, label=label)
            return self._fig_to_bitmap(fig)
        except Exception as exc:
            if self.debug:
                print(f"CFD slice plot error: {exc}")
            return None

    def plot_cfd_3d(self, mesh, result):
        """Render a down-sampled 3D air/solid temperature field."""
        try:
            values, label, cmap = self._cfd_field(mesh, result, 'TEMPERATURE')
            indices = np.argwhere(np.isfinite(values))
            if len(indices) > 40000:
                indices = indices[::int(np.ceil(len(indices) / 40000.0))]
            dx, dy, dz = mesh.spacing_m
            coords = (indices + 0.5) * np.asarray([dx, dy, dz]) * 1000.0
            colors = values[tuple(indices.T)]
            fig = plt.figure(figsize=(8, 6), constrained_layout=True)
            axis = fig.add_subplot(111, projection='3d')
            scatter = axis.scatter(coords[:, 0], coords[:, 1], coords[:, 2], c=colors,
                                   cmap=cmap, s=4, alpha=0.45)
            axis.set_xlabel('X (mm)'); axis.set_ylabel('Y (mm)'); axis.set_zlabel('Z (mm)')
            axis.set_title('Enclosure CFD volumetric temperature')
            fig.colorbar(scatter, ax=axis, label=label, shrink=0.75)
            return self._fig_to_bitmap(fig)
        except Exception as exc:
            if self.debug:
                print(f"CFD 3D plot error: {exc}")
            return None

    def plot_cfd_residuals(self, result):
        try:
            fig, axis = plt.subplots(figsize=(8, 5), constrained_layout=True)
            for label, values in (
                ('Continuity', result.residuals.continuity),
                ('Momentum', result.residuals.momentum),
                ('Energy', result.residuals.energy),
            ):
                finite = np.asarray(values, dtype=float)
                finite = np.where(np.isfinite(finite), finite, np.nan)
                axis.semilogy(np.arange(1, len(finite) + 1), np.maximum(finite, 1e-16), label=label)
            axis.set_xlabel('Iteration'); axis.set_ylabel('Residual')
            axis.set_title('Enclosure CFD convergence')
            axis.grid(True, which='both', alpha=0.3); axis.legend()
            return self._fig_to_bitmap(fig)
        except Exception as exc:
            if self.debug:
                print(f"CFD residual plot error: {exc}")
            return None

    def _fig_to_bitmap(self, fig):
        return self.bitmap_from_png(self._fig_to_png(fig))

    def _fig_to_png(self, fig):
        """Render a figure to immutable PNG bytes without touching wx."""
        buf = io.BytesIO()
        try:
            fig.savefig(buf, format='png', dpi=160)
            return buf.getvalue()
        finally:
            plt.close(fig)

    @staticmethod
    def bitmap_from_png(png_bytes):
        """Create wx objects on the GUI thread from background-rendered PNG."""
        buf = io.BytesIO(png_bytes)
        image = wx.Image(buf, wx.BITMAP_TYPE_PNG)
        return wx.Bitmap(image)
