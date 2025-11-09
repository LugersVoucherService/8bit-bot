import json
import numpy as np
import os
from typing import List, Dict, Tuple
import base64

class GLTFRenderer:
    def __init__(self, build_file_path: str):
        self.build_file_path = build_file_path
        self.blocks = []
        self.positions = []
        self.rotations = []
        self.sizes = []
        self.colors = []
        self.transparencies = []
        
    def parse_build_file(self):
        """Parse the build file"""
        print(f"Parsing build file: {self.build_file_path}")
        
        with open(self.build_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check if it's Format 1: Custom text format (kurma.Build)
        if '/' in content and ':' in content and not content.strip().startswith('[') and not content.strip().startswith('{'):
            print("Detected custom text format (kurma.Build style)")
            block_count = 0
            blocks = content.split('/')
            
            for block_str in blocks:
                if not block_str.strip():
                    continue
                
                try:
                    fields = block_str.split(':')
                    if len(fields) >= 5:
                        position_str = fields[0].strip()
                        rotation_str = fields[1].strip()
                        color_str = fields[2].strip()
                        size_str = fields[3].strip()
                        block_type = fields[4].strip()
                        
                        try:
                            position = [float(x.strip()) for x in position_str.split(',')]
                            if len(position) == 3:
                                self.positions.append(np.array(position))
                            else:
                                self.positions.append(np.array([0.0, 0.0, 0.0]))
                        except:
                            self.positions.append(np.array([0.0, 0.0, 0.0]))
                        
                        try:
                            rotation = [float(x.strip()) for x in rotation_str.split(',')]
                            if len(rotation) == 3:
                                self.rotations.append(np.array(rotation))
                            else:
                                self.rotations.append(np.array([0.0, 0.0, 0.0]))
                        except:
                            self.rotations.append(np.array([0.0, 0.0, 0.0]))
                        
                        try:
                            size = [float(x.strip()) for x in size_str.split(',')]
                            if len(size) == 3:
                                size = [abs(float(s)) for s in size]
                                size = [max(0.01, min(10000.0, s)) for s in size]
                                self.sizes.append(np.array(size))
                            else:
                                self.sizes.append(np.array([1.0, 1.0, 1.0]))
                        except:
                            self.sizes.append(np.array([1.0, 1.0, 1.0]))
                        
                        try:
                            color = [float(x.strip()) for x in color_str.split(',')]
                            if len(color) >= 3:
                                color = tuple(color[:3])
                            else:
                                color = self._get_default_color(block_type)
                        except:
                            color = self._get_default_color(block_type)
                        
                        self.colors.append(color)
                        self.transparencies.append(0.0)
                        self.blocks.append({'type': block_type, 'data': block_str})
                        block_count += 1
                except Exception as e:
                    print(f"Warning: Error parsing block in text format: {e}")
                    continue
            
            print(f"Parsed {block_count} blocks from custom text format")
            return
        
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            print(f"JSON parsing error: {e}")
            return
        
        if isinstance(data, list):
            # Check if it's Format 4: List with block type names and dict (ani.Build format)
            # Format: [["BlockType1", "BlockType2", ...], {"BlockType1": [{...}, {...}], ...}]
            if len(data) == 2 and isinstance(data[0], list) and isinstance(data[1], dict):
                print("Detected list-with-block-types format (ani.Build style)")
                data = data[1]  # Use the dict part (second element)
            elif len(data) > 0 and isinstance(data[0], list):
                # Format 2: List of lists
                print("Detected list-of-lists format (tank.build style)")
                block_count = 0
                for block_data in data:
                    if isinstance(block_data, list) and len(block_data) >= 7:
                        try:
                            # Parse list format: [type, position, rotation, ?, size, anchored, color, ...]
                            block_type = str(block_data[0]) if len(block_data) > 0 else "UnknownBlock"
                            position = block_data[1] if len(block_data) > 1 and isinstance(block_data[1], list) else [0, 0, 0]
                            rotation = block_data[2] if len(block_data) > 2 and isinstance(block_data[2], list) else [0, 0, 0]
                            size = block_data[4] if len(block_data) > 4 and isinstance(block_data[4], list) else [1, 1, 1]
                            color = block_data[6] if len(block_data) > 6 else [128, 128, 128]
                            
                            if len(position) == 3:
                                self.positions.append(np.array([float(p) for p in position]))
                            else:
                                self.positions.append(np.array([0.0, 0.0, 0.0]))
                            
                            if len(rotation) == 3:
                                self.rotations.append(np.array([float(r) for r in rotation]))
                            else:
                                self.rotations.append(np.array([0.0, 0.0, 0.0]))
                            
                            if len(size) == 3:
                                size = [abs(float(s)) for s in size]
                                size = [max(0.01, min(10000.0, s)) for s in size]
                                self.sizes.append(np.array(size))
                            else:
                                self.sizes.append(np.array([1.0, 1.0, 1.0]))
                            
                            if isinstance(color, list) and len(color) >= 3:
                                color = tuple(float(c) / 255.0 if c > 1 else float(c) for c in color[:3])
                            else:
                                color = self._parse_color(color) if isinstance(color, str) else self._get_default_color(block_type)
                            
                            self.colors.append(color)
                            self.transparencies.append(0.0) 
                            self.blocks.append({'type': block_type, 'data': block_data})
                            block_count += 1
                        except Exception as e:
                            print(f"Warning: Error parsing block in list format: {e}, using defaults")
                            # Still add defaults to keep arrays in sync
                            self.positions.append(np.array([0.0, 0.0, 0.0]))
                            self.rotations.append(np.array([0.0, 0.0, 0.0]))
                            self.sizes.append(np.array([1.0, 1.0, 1.0]))
                            self.colors.append((0.5, 0.5, 0.5))
                            self.transparencies.append(0.0)
                            self.blocks.append({'type': 'UnknownBlock', 'data': block_data})
                            block_count += 1
                
                print(f"Parsed {block_count} blocks from list-of-lists format")
                return
            
            elif len(data) > 1 and isinstance(data[1], dict):
                data = data[1]
            elif len(data) > 0 and isinstance(data[0], dict):
                data = data[0]
            else:
                print("Error: Could not find block data dictionary in list structure")
                return
        
        print("Detected dict-of-block-types format (7R9R42YYMGNT.Build style)")
        block_count = 0
        for block_type, block_list in data.items():
            if isinstance(block_list, list):
                for block in block_list:
                    if isinstance(block, dict):
                        try:
                            # Parse position - handle both string and list formats
                            position_str = block.get('Position', '0, 0, 0')
                            if isinstance(position_str, str):
                                try:
                                    pos = [float(x.strip()) for x in position_str.split(',')]
                                    if len(pos) != 3:
                                        pos = [0.0, 0.0, 0.0]
                                except (ValueError, AttributeError):
                                    pos = [0.0, 0.0, 0.0]
                            elif isinstance(position_str, (list, tuple)):
                                try:
                                    pos = [float(p) for p in position_str[:3]]
                                    if len(pos) != 3:
                                        pos = [0.0, 0.0, 0.0]
                                except (ValueError, TypeError):
                                    pos = [0.0, 0.0, 0.0]
                            else:
                                pos = [0.0, 0.0, 0.0]
                            
                            self.positions.append(np.array(pos))
                            
                            # Parse rotation - handle both string and list formats
                            rotation_str = block.get('Rotation', '0, 0, 0')
                            if isinstance(rotation_str, str):
                                try:
                                    rot = [float(x.strip()) for x in rotation_str.split(',')]
                                    if len(rot) != 3:
                                        rot = [0.0, 0.0, 0.0]
                                except (ValueError, AttributeError):
                                    rot = [0.0, 0.0, 0.0]
                            elif isinstance(rotation_str, (list, tuple)):
                                try:
                                    rot = [float(r) for r in rotation_str[:3]]
                                    if len(rot) != 3:
                                        rot = [0.0, 0.0, 0.0]
                                except (ValueError, TypeError):
                                    rot = [0.0, 0.0, 0.0]
                            else:
                                rot = [0.0, 0.0, 0.0]
                            
                            self.rotations.append(np.array(rot))
                            
                            # Parse size - handle both string and list formats
                            size_str = block.get('Size', '1, 1, 1')
                            if isinstance(size_str, str):
                                try:
                                    size = [float(x.strip()) for x in size_str.split(',')]
                                    if len(size) == 3:
                                        size = [abs(float(s)) for s in size]
                                        size = [max(0.01, min(10000.0, s)) for s in size]
                                    else:
                                        size = [1.0, 1.0, 1.0]
                                except Exception as e:
                                    print(f"Warning: Could not parse size '{size_str}': {e}, using default [1, 1, 1]")
                                    size = [1.0, 1.0, 1.0]
                            elif isinstance(size_str, (list, tuple)):
                                try:
                                    size = [float(s) for s in size_str[:3]]
                                    if len(size) == 3:
                                        size = [abs(float(s)) for s in size]
                                        size = [max(0.01, min(10000.0, s)) for s in size]
                                    else:
                                        size = [1.0, 1.0, 1.0]
                                except (ValueError, TypeError):
                                    size = [1.0, 1.0, 1.0]
                            else:
                                size = [1.0, 1.0, 1.0]
                            
                            self.sizes.append(np.array(size))
                            
                            # Parse color
                            color = block.get('Color', None)
                            if color is None:
                                color = self._get_default_color(block_type)
                            else:
                                color = self._parse_color(color)
                            self.colors.append(color)
                            
                            # Parse transparency
                            transparency = block.get('Transparency', 0)
                            try:
                                transparency = float(transparency)
                            except (ValueError, TypeError):
                                transparency = 0.0
                            self.transparencies.append(transparency)
                            
                            self.blocks.append({
                                'type': block_type,
                                'position': pos,
                                'rotation': rot,
                                'size': size
                            })
                            block_count += 1
                        except Exception as e:
                            print(f"Warning: Error parsing block {block_type}: {e}, using defaults")
                            # Still add defaults to keep arrays in sync
                            self.positions.append(np.array([0.0, 0.0, 0.0]))
                            self.rotations.append(np.array([0.0, 0.0, 0.0]))
                            self.sizes.append(np.array([1.0, 1.0, 1.0]))
                            self.colors.append(self._get_default_color(block_type))
                            self.transparencies.append(0.0)
                            self.blocks.append({
                                'type': block_type,
                                'position': [0.0, 0.0, 0.0],
                                'rotation': [0.0, 0.0, 0.0],
                                'size': [1.0, 1.0, 1.0]
                            })
                            block_count += 1
        
        if len(self.positions) == 0:
            print("Warning: No blocks with valid positions found!")
            return
        
        # Validate array synchronization
        array_lengths = {
            'positions': len(self.positions),
            'rotations': len(self.rotations),
            'sizes': len(self.sizes),
            'colors': len(self.colors),
            'transparencies': len(self.transparencies),
            'blocks': len(self.blocks)
        }
        
        if len(set(array_lengths.values())) > 1:
            print(f"WARNING: Array length mismatch! {array_lengths}")
            print("This could cause rendering issues. Attempting to fix...")
            # Find the minimum length and truncate all arrays to match
            min_len = min(array_lengths.values())
            if min_len > 0:
                self.positions = self.positions[:min_len]
                self.rotations = self.rotations[:min_len]
                self.sizes = self.sizes[:min_len]
                self.colors = self.colors[:min_len]
                self.transparencies = self.transparencies[:min_len]
                self.blocks = self.blocks[:min_len]
                print(f"Truncated all arrays to length {min_len}")
        
        print(f"Parsed {block_count} blocks")
        print(f"Array lengths: {array_lengths}")
    
    def _get_default_color(self, block_type: str) -> Tuple[float, float, float]:
        color_map = {
            'BuildingBlock': (0.7, 0.7, 0.7),
            'GoldBlock': (0.9, 0.8, 0.2),
            'TitaniumBlock': (0.6, 0.6, 0.8),
            'PlasticBlock': (0.8, 0.8, 0.9),
            'ConcreteBlock': (0.6, 0.6, 0.6),
            'MetalBlock': (0.5, 0.5, 0.5),
            'Piston': (0.7, 0.3, 0.3),
            'FrontWheel': (0.2, 0.2, 0.2),
            'BackWheel': (0.2, 0.2, 0.2),
            'SpikeTrap': (0.8, 0.2, 0.2),
            'Portal': (0.2, 0.8, 0.8),
            'Seat': (0.8, 0.2, 0.2),
            'Glue': (0.9, 0.9, 0.1),
            'Rope': (0.5, 0.3, 0.1),
            'Hinge': (0.8, 0.8, 0.2),
            'TitaniumRod': (0.6, 0.6, 0.8),
            'CameraDome': (0.2, 0.8, 0.2),
            'CarSeat': (0.8, 0.2, 0.2),
            # Wood blocks - light brown wooden boxes (game blocks)
            'WoodBlock': (0.75, 0.65, 0.50),  # Light brown wood color
            'Wood': (0.75, 0.65, 0.50),
            'GameBlock': (0.75, 0.65, 0.50),  # Game blocks are often wood
        }
        return color_map.get(block_type, (0.5, 0.5, 0.5))
    
    def _parse_color(self, color_value) -> Tuple[float, float, float]:
        if color_value is None:
            return (0.5, 0.5, 0.5)
        
        if isinstance(color_value, (list, tuple)):
            if len(color_value) >= 3:
                return tuple(float(c) / 255.0 if c > 1 else float(c) for c in color_value[:3])
        
        if isinstance(color_value, str):
            try:
                parts = [float(x.strip()) for x in color_value.split(',')]
                if len(parts) >= 3:
                    return tuple(c / 255.0 if c > 1 else c for c in parts[:3])
            except:
                pass
        
        return (0.5, 0.5, 0.5)
    
    def _build_roblox_rotation_matrix(self, rotation_deg: np.ndarray) -> np.ndarray:
        """
        Build rotation matrix that exactly matches Roblox CFrame.angles(rx, ry, rz).
        
        Roblox uses YXZ rotation order in a left-handed coordinate system.
        To convert to right-handed (GLTF), we:
        1. Negate the Z rotation angle (flip handedness)
        2. Apply rotations in YXZ order for right-handed system
        """
        rx, ry, rz = np.radians(rotation_deg)
        
        # Convert from left-handed to right-handed by negating Z rotation
        rz_rh = -rz
        
        # Build rotation matrices for right-handed Y-up coordinate system
        cosx, sinx = np.cos(rx), np.sin(rx)
        cosy, siny = np.cos(ry), np.sin(ry)
        cosz, sinz = np.cos(rz_rh), np.sin(rz_rh)
        
        # Y rotation (yaw) - right-handed
        Ry = np.array([
            [cosy, 0, -siny],
            [0, 1, 0],
            [siny, 0, cosy]
        ], dtype=np.float32)
        
        # X rotation (pitch) - right-handed
        Rx = np.array([
            [1, 0, 0],
            [0, cosx, sinx],
            [0, -sinx, cosx]
        ], dtype=np.float32)
        
        # Z rotation (roll) - right-handed
        Rz = np.array([
            [cosz, sinz, 0],
            [-sinz, cosz, 0],
            [0, 0, 1]
        ], dtype=np.float32)
        
        # Apply in YXZ order: Ry * Rx * Rz
        R = Ry @ Rx @ Rz
        
        return R
    
    def compute_scaled_counts(self) -> Dict[str, int]:
        """
        Compute scaled counts for each block type based on volume.
        Similar to computeScaledCounts in build.js
        Returns: { block_type: scaled_count }
        """
        counts = {}
        
        # Group blocks by type
        block_groups = {}
        for i, block in enumerate(self.blocks):
            block_type = block.get('type', 'UnknownBlock')
            if block_type not in block_groups:
                block_groups[block_type] = []
            block_groups[block_type].append(i)
        
        # Calculate scaled counts for each block type
        for block_type, indices in block_groups.items():
            total = 0
            for idx in indices:
                if idx < len(self.sizes):
                    size = self.sizes[idx]
                    if isinstance(size, np.ndarray):
                        size = size.tolist()
                    elif not isinstance(size, (list, tuple)):
                        size = [1.0, 1.0, 1.0]
                    
                    x, y, z = float(size[0]) if len(size) > 0 else 1.0, \
                             float(size[1]) if len(size) > 1 else 1.0, \
                             float(size[2]) if len(size) > 2 else 1.0
                    volume = x * y * z
                    total += int(np.ceil(volume / 8))
                else:
                    total += 1  # Default size [1,1,1] = 1 scaled count
            
            counts[block_type] = total
        
        return counts
    
    def summarise_blocks(self) -> Dict[str, Dict[str, float]]:
        """
        Produce a quick summary: count, total raw volume and total scaled count.
        Similar to summariseBlocks in build.js
        Returns: { block_type: { count, rawVolume, scaledCount } }
        """
        summary = {}
        
        # Group blocks by type
        block_groups = {}
        for i, block in enumerate(self.blocks):
            block_type = block.get('type', 'UnknownBlock')
            if block_type not in block_groups:
                block_groups[block_type] = []
            block_groups[block_type].append(i)
        
        # Calculate summary for each block type
        for block_type, indices in block_groups.items():
            count = 0
            raw_volume = 0.0
            scaled = 0
            
            for idx in indices:
                count += 1
                
                if idx < len(self.sizes):
                    size = self.sizes[idx]
                    if isinstance(size, np.ndarray):
                        size = size.tolist()
                    elif not isinstance(size, (list, tuple)):
                        size = [1.0, 1.0, 1.0]
                    
                    x, y, z = float(size[0]) if len(size) > 0 else 1.0, \
                             float(size[1]) if len(size) > 1 else 1.0, \
                             float(size[2]) if len(size) > 2 else 1.0
                    vol = x * y * z
                    raw_volume += vol
                    scaled += int(np.ceil(vol / 8))
                else:
                    # Default size [1,1,1]
                    raw_volume += 1.0
                    scaled += 1
            
            summary[block_type] = {
                'count': count,
                'rawVolume': raw_volume,
                'scaledCount': scaled
            }
        
        return summary
    
    def export_to_gltf(self, output_path: str):
        if len(self.positions) == 0:
            print("No blocks to export!")
            return
        
        print(f"Exporting to GLTF: {output_path}")
        
        # Calculate center from positions (in left-handed system)
        positions_array = np.array(self.positions)
        min_bounds = positions_array.min(axis=0)
        max_bounds = positions_array.max(axis=0)
        center_lh = (min_bounds + max_bounds) / 2
        size = max_bounds - min_bounds
        max_size = np.max(size)
        
        # Convert center from left-handed to right-handed (flip Z)
        center = center_lh.copy()
        center[2] = -center[2]
        
        gltf = {
            "asset": {
                "version": "2.0",
                "generator": "Build Renderer"
            },
            "scene": 0,
            "scenes": [{
                "nodes": [0]
            }],
            "nodes": [{
                "mesh": 0
            }],
            "meshes": [{
                "primitives": []
            }],
            "materials": [],
            "accessors": [],
            "bufferViews": [],
            "buffers": []
        }
        
        material_map = {}
        material_groups = {}  
        
        for i in range(len(self.positions)):
            color = self.colors[i] if i < len(self.colors) else (0.5, 0.5, 0.5)
            transparency = self.transparencies[i] if i < len(self.transparencies) else 0
            material_key = (float(color[0]), float(color[1]), float(color[2]), float(transparency))
            
            if material_key not in material_map:
                material_idx = len(gltf["materials"])
                material_map[material_key] = material_idx
                material_groups[material_idx] = []
                
                alpha = 1.0 - transparency
                gltf["materials"].append({
                    "name": f"Material_{material_idx}",
                    "pbrMetallicRoughness": {
                        "baseColorFactor": [color[0], color[1], color[2], alpha],
                        "metallicFactor": 0.1,  # Slight metallic for smoother look
                        "roughnessFactor": 0.4  # Lower roughness for smoother, less rigid appearance
                    },
                    "doubleSided": True  
                })
                if alpha < 1.0:
                    gltf["materials"][-1]["alphaMode"] = "BLEND"
            
            material_groups[material_map[material_key]].append(i)
        
        primitives = []
        buffer_offset = 0
        
        for material_idx in sorted(material_groups.keys()):
            block_indices = material_groups[material_idx]
            group_vertices = []
            group_normals = []
            group_indices = []
            
            for block_idx in block_indices:
                # Validate index bounds
                if block_idx >= len(self.positions):
                    print(f"WARNING: block_idx {block_idx} >= len(positions) {len(self.positions)}, skipping")
                    continue
                if block_idx >= len(self.sizes):
                    print(f"WARNING: block_idx {block_idx} >= len(sizes) {len(self.sizes)}, using default")
                if block_idx >= len(self.rotations):
                    print(f"WARNING: block_idx {block_idx} >= len(rotations) {len(self.rotations)}, using default")
                
                # Get position and rotation from Roblox
                position_roblox = self.positions[block_idx].copy()
                size = self.sizes[block_idx] if block_idx < len(self.sizes) else np.array([1.0, 1.0, 1.0])
                rotation = self.rotations[block_idx] if block_idx < len(self.rotations) else np.array([0, 0, 0])
                
                if not isinstance(size, np.ndarray):
                    size = np.array(size)
                if size.shape[0] != 3:
                    size = np.array([1.0, 1.0, 1.0])
                
                size = np.abs(size)
                size = np.clip(size, 0.01, 10000.0)
                
                # Convert position from Roblox (left-handed) to GLTF (right-handed)
                # Roblox: +X right, +Y up, +Z forward (left-handed)
                # GLTF: +X right, +Y up, +Z backward (right-handed)
                position = position_roblox.copy()
                position[2] = -position[2]  # Flip Z axis
                
                # Build cube corners in local space (before rotation)
                half_size = size / 2.0
                corners_local = np.array([
                    [-half_size[0], -half_size[1], -half_size[2]],  # 0
                    [ half_size[0], -half_size[1], -half_size[2]],  # 1
                    [ half_size[0],  half_size[1], -half_size[2]],  # 2
                    [-half_size[0],  half_size[1], -half_size[2]],  # 3
                    [-half_size[0], -half_size[1],  half_size[2]],  # 4
                    [ half_size[0], -half_size[1],  half_size[2]],  # 5
                    [ half_size[0],  half_size[1],  half_size[2]],  # 6
                    [-half_size[0],  half_size[1],  half_size[2]],  # 7
                ], dtype=np.float32)
                
                # Get rotation matrix that matches Roblox CFrame.angles
                R = self._build_roblox_rotation_matrix(rotation)
                
                # Apply rotation and translation to corners
                corners = (corners_local @ R.T) + position

                # Face definitions with normals (4 vertices per face, 6 faces = 24 vertices)
                face_defs = [
                    ([0, 1, 2, 3], [0, 0, -1]),   # Back (-Z)
                    ([4, 7, 6, 5], [0, 0, 1]),    # Front (+Z)
                    ([0, 4, 5, 1], [0, -1, 0]),   # Bottom (-Y)
                    ([2, 6, 7, 3], [0, 1, 0]),    # Top (+Y)
                    ([0, 3, 7, 4], [-1, 0, 0]),   # Left (-X)
                    ([1, 5, 6, 2], [1, 0, 0]),    # Right (+X)
                ]
                
                base_vertex_idx = len(group_vertices)
                
                # Get rotation matrix for transforming normals
                R = self._build_roblox_rotation_matrix(rotation)
                
                for corner_indices, normal_base in face_defs:
                    # Transform normal by rotation matrix
                    normal_transformed = (R @ np.array(normal_base)).tolist()

                    for corner_idx in corner_indices:
                        group_vertices.append(corners[corner_idx].tolist())
                        group_normals.append(normal_transformed)
                
                for face_idx in range(6):
                    v0 = base_vertex_idx + face_idx * 4
                    group_indices.extend([
                        v0, v0 + 1, v0 + 2,  # First triangle
                        v0, v0 + 2, v0 + 3   # Second triangle
                    ])
            
            if not group_vertices:
                continue
            
            vertices_array = np.array(group_vertices, dtype=np.float32)
            normals_array = np.array(group_normals, dtype=np.float32)
            indices_array = np.array(group_indices, dtype=np.uint32)
            vertices_bytes = vertices_array.tobytes()
            normals_bytes = normals_array.tobytes()
            indices_bytes = indices_array.tobytes()
            
            def pad_buffer(buf):
                padding = (4 - (len(buf) % 4)) % 4
                return buf + b'\x00' * padding
            
            vertices_bytes = pad_buffer(vertices_bytes)
            normals_bytes = pad_buffer(normals_bytes)
            indices_bytes = pad_buffer(indices_bytes)
            buffer_data = vertices_bytes + normals_bytes + indices_bytes
            buffer_base64 = base64.b64encode(buffer_data).decode('ascii')
            
            buffer_idx = len(gltf["buffers"])
            gltf["buffers"].append({
                "uri": f"data:application/octet-stream;base64,{buffer_base64}",
                "byteLength": len(buffer_data)
            })
            
            offset = 0
            vertices_view_idx = len(gltf["bufferViews"])
            gltf["bufferViews"].append({
                "buffer": buffer_idx,
                "byteOffset": offset,
                "byteLength": len(vertices_bytes),
                "target": 34962  # ARRAY_BUFFER
            })
            offset += len(vertices_bytes)
            
            normals_view_idx = len(gltf["bufferViews"])
            gltf["bufferViews"].append({
                "buffer": buffer_idx,
                "byteOffset": offset,
                "byteLength": len(normals_bytes),
                "target": 34962  # ARRAY_BUFFER
            })
            offset += len(normals_bytes)
            
            indices_view_idx = len(gltf["bufferViews"])
            gltf["bufferViews"].append({
                "buffer": buffer_idx,
                "byteOffset": offset,
                "byteLength": len(indices_bytes),
                "target": 34963  # ELEMENT_ARRAY_BUFFER
            })
            
            vertices_accessor_idx = len(gltf["accessors"])
            gltf["accessors"].append({
                "bufferView": vertices_view_idx,
                "byteOffset": 0,
                "componentType": 5126,  # FLOAT
                "count": len(vertices_array),
                "type": "VEC3",
                "max": vertices_array.max(axis=0).tolist(),
                "min": vertices_array.min(axis=0).tolist()
            })
            
            normals_accessor_idx = len(gltf["accessors"])
            gltf["accessors"].append({
                "bufferView": normals_view_idx,
                "byteOffset": 0,
                "componentType": 5126,  # FLOAT
                "count": len(normals_array),
                "type": "VEC3"
            })
            
            indices_accessor_idx = len(gltf["accessors"])
            gltf["accessors"].append({
                "bufferView": indices_view_idx,
                "byteOffset": 0,
                "componentType": 5125,  # UNSIGNED_INT
                "count": len(indices_array),
                "type": "SCALAR"
            })
            
            primitives.append({
                "attributes": {
                    "POSITION": vertices_accessor_idx,
                    "NORMAL": normals_accessor_idx
                },
                "indices": indices_accessor_idx,
                "material": material_idx
            })
        
        gltf["meshes"][0]["primitives"] = primitives
        if len(gltf["materials"]) == 0:
            gltf["materials"].append({
                "name": "DefaultMaterial",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.5, 0.5, 0.5, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.7
                }
            })
        
        with open(output_path, 'w') as f:
            json.dump(gltf, f, indent=2)
        
        print(f"Exported {len(self.positions)} blocks to GLTF")
        return center, max_size
    
    def create_viewer_html(self, gltf_filename: str, center: np.ndarray, max_size: float, port: int = 8000):
        cx, cy, cz = float(center[0]), float(center[1]), float(center[2])
        distance = max_size * 2
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>3D Build Viewer</title>
    <style>
        body {{
            margin: 0;
            padding: 0;
            overflow: hidden;
            background: #1a1a1a;
            font-family: Arial, sans-serif;
        }}
        #container {{
            width: 100vw;
            height: 100vh;
        }}
        #info {{
            position: absolute;
            top: 10px;
            left: 10px;
            color: white;
            background: rgba(0, 0, 0, 0.7);
            padding: 10px;
            border-radius: 5px;
            z-index: 100;
        }}
        #controls {{
            position: absolute;
            bottom: 10px;
            left: 10px;
            color: white;
            background: rgba(0, 0, 0, 0.7);
            padding: 10px;
            border-radius: 5px;
            z-index: 100;
        }}
    </style>
    <script type="importmap">
        {{
            "imports": {{
                "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
                "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
            }}
        }}
    </script>
</head>
<body>
    <div id="container"></div>
    <div id="info">
        <h3>3D Build Viewer</h3>
        <p>Drag to rotate | Scroll to zoom | Right-click to pan</p>
    </div>
    <div id="controls">
        <button onclick="resetCamera()">Reset Camera</button>
        <button onclick="toggleWireframe()">Toggle Wireframe</button>
        <button onclick="takeAllSnapshots()">Take Snapshots</button>
    </div>
    
    <script type="module">
        import * as THREE from 'three';
        import {{ GLTFLoader }} from 'three/addons/loaders/GLTFLoader.js';
        import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
        
        // Initialize viewer
        let scene, camera, renderer, controls, model;
        let wireframe = false;
        
        // Scene setup
        scene = new THREE.Scene();
        scene.background = new THREE.Color(0x1a1a1a);
        
        // Camera setup - position above and in front of the model
        const distance = {distance};
        camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, distance * 10);
        // Position camera at an angle above and in front of the model (further away)
        const cameraDistance = distance * 2.5;  // Increased initial distance
        camera.position.set({cx} + cameraDistance * 0.7, {cy} + cameraDistance * 0.5, {cz} + cameraDistance * 0.7);
        camera.lookAt({cx}, {cy}, {cz});
        
        // Renderer setup with high quality settings
        renderer = new THREE.WebGLRenderer({{
            antialias: true,
            powerPreference: "high-performance",
            stencil: false,
            depth: true
        }});
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2)); // High DPI support, capped at 2x for performance
        renderer.setSize(window.innerWidth, window.innerHeight);
        renderer.shadowMap.enabled = true;
        renderer.shadowMap.type = THREE.PCFSoftShadowMap; // Soft shadows
        renderer.toneMapping = THREE.ACESFilmicToneMapping;
        renderer.toneMappingExposure = 1.0;
        renderer.outputColorSpace = THREE.SRGBColorSpace;
        renderer.physicallyCorrectLights = true;
        document.getElementById('container').appendChild(renderer.domElement);
        
        // Controls with improved zoom behavior
        controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;
        controls.dampingFactor = 0.05;
        controls.target.set({cx}, {cy}, {cz});
        
        // Regular zoom behavior
        controls.zoomSpeed = 1.0;  // Regular zoom speed
        controls.minDistance = 0.1;  // Can zoom in very close
        controls.maxDistance = distance * 5;  // Can zoom out further
        controls.enablePan = true;  // Allow panning
        controls.panSpeed = 0.8;  // Pan speed
        
        // Enhanced Lighting for smoother, less rigid appearance
        const ambientLight = new THREE.AmbientLight(0xffffff, 1.0);  // Increased ambient for softer shadows
        scene.add(ambientLight);
        
        // Main directional light - softer
        const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
        directionalLight.position.set(50, 80, 50);
        directionalLight.castShadow = true;
        scene.add(directionalLight);
        
        // Additional lights for smoother shading
        const directionalLight2 = new THREE.DirectionalLight(0xffffff, 0.6);
        directionalLight2.position.set(-50, 50, -50);
        scene.add(directionalLight2);
        
        // Fill light from below to reduce harsh shadows
        const directionalLight3 = new THREE.DirectionalLight(0xffffff, 0.4);
        directionalLight3.position.set(0, -50, 0);
        scene.add(directionalLight3);
        
        // Point light for additional smoothness
        const pointLight = new THREE.PointLight(0xffffff, 0.6);
        pointLight.position.set(0, 100, 0);
        scene.add(pointLight);
        
        // Load GLTF model
        const loader = new GLTFLoader();
        console.log('Loading GLTF file: {gltf_filename}');
        loader.load('{gltf_filename}', function(gltf) {{
            console.log('GLTF loaded successfully:', gltf);
            model = gltf.scene;
            scene.add(model);
            
            // Center and scale model
            const box = new THREE.Box3().setFromObject(model);
            box.expandByObject(model);
            const center = box.getCenter(new THREE.Vector3());
            const size = box.getSize(new THREE.Vector3());
            
            console.log('Model bounds:', box);
            console.log('Center:', center);
            console.log('Size:', size);
            console.log('Model children:', model.children.length);
            
            // Update camera to fit model - zoom in more on load
            if (size.length() > 0) {{
                const maxDim = Math.max(size.x, size.y, size.z);
                // Calculate distance to fit model in viewport (zoom in more)
                // Use a smaller multiplier to zoom in closer
                const distance = maxDim * 0.9;  // Reduced to 0.9 for very close zoom
                // Position camera at an angle above and in front
                camera.position.set(
                    center.x + distance * 0.7,
                    center.y + distance * 0.5,
                    center.z + distance * 0.7
                );
                camera.lookAt(center);
                controls.target.copy(center);
                controls.update();
            }}
        }}, function(progress) {{
            console.log('Loading progress:', progress);
        }}, function(error) {{
            console.error('Error loading model:', error);
            document.getElementById('info').innerHTML = '<h3>Error Loading Model</h3><p>' + error + '</p>';
        }});
        
        // Animation loop
        function animate() {{
            requestAnimationFrame(animate);
            controls.update();
            renderer.render(scene, camera);
        }}
        animate();
        
        // Window resize
        window.addEventListener('resize', function() {{
            camera.aspect = window.innerWidth / window.innerHeight;
            camera.updateProjectionMatrix();
            renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
            renderer.setSize(window.innerWidth, window.innerHeight);
        }});
        
        // Snapshot function
        function takeSnapshot(viewName) {{
            if (!model) return;
            
            // Get model bounds
            const box = new THREE.Box3().setFromObject(model);
            box.expandByObject(model);
            const center = box.getCenter(new THREE.Vector3());
            const size = box.getSize(new THREE.Vector3());
            const maxDim = Math.max(size.x, size.y, size.z);
            const distance = maxDim * 0.9;  // Use same close distance as initial load
            
            // Calculate camera position based on view
            let camPos;
            if (viewName === 'front_30deg') {{
                // 30 degree perspective from front (rotated 30 degrees around Y axis)
                const angleRad = 30 * Math.PI / 180;
                camPos = new THREE.Vector3(
                    center.x + distance * Math.sin(angleRad),
                    center.y + distance * 0.3,  // Slightly elevated
                    center.z + distance * Math.cos(angleRad)
                );
            }} else if (viewName === 'side') {{
                // Regular side view (from right side)
                camPos = new THREE.Vector3(center.x + distance, center.y, center.z);
            }} else if (viewName === 'front') {{
                // Front view (looking along +Z axis)
                camPos = new THREE.Vector3(center.x, center.y, center.z + distance);
            }} else if (viewName === 'back') {{
                // Back view (looking along -Z axis)
                camPos = new THREE.Vector3(center.x, center.y, center.z - distance);
            }} else {{
                return;
            }}
            
            // Set camera position and update
            camera.position.copy(camPos);
            camera.lookAt(center);
            controls.target.copy(center);
            controls.update();
            
            // Wait a frame for camera to update, then capture
            requestAnimationFrame(() => {{
                requestAnimationFrame(() => {{
                    // Render frame
                    renderer.render(scene, camera);
                    
                    // Capture screenshot
                    const dataURL = renderer.domElement.toDataURL('image/png');
                    
                    // Download image
                    const link = document.createElement('a');
                    const filename = '{gltf_filename}'.replace('.gltf', '') + '_' + viewName + '.png';
                    link.download = filename;
                    link.href = dataURL;
                    link.click();
                    console.log('Snapshot saved: ' + filename);
                }});
            }});
        }}
        
        // Take all snapshots
        function takeAllSnapshots() {{
            if (!model) {{
                alert('Model not loaded yet!');
                return;
            }}
            console.log('Taking snapshots...');
            // Space out snapshots to allow camera to update
            setTimeout(() => takeSnapshot('front_30deg'), 100);
            setTimeout(() => takeSnapshot('side'), 2000);
            setTimeout(() => takeSnapshot('front'), 4000);
            setTimeout(() => takeSnapshot('back'), 6000);
        }}
        
        // Control functions
        function resetCamera() {{
            const resetDistance = {distance} * 2.5;  // Increased reset distance
            camera.position.set({cx} + resetDistance * 0.7, {cy} + resetDistance * 0.5, {cz} + resetDistance * 0.7);
            camera.lookAt({cx}, {cy}, {cz});
            controls.target.set({cx}, {cy}, {cz});
            controls.update();
        }}
        
        function toggleWireframe() {{
            wireframe = !wireframe;
            if (model) {{
                model.traverse(function(child) {{
                    if (child.isMesh) {{
                        child.material.wireframe = wireframe;
                    }}
                }});
            }}
        }}
        
        // Make functions global for button onclick handlers
        window.resetCamera = resetCamera;
        window.toggleWireframe = toggleWireframe;
        window.takeAllSnapshots = takeAllSnapshots;
    </script>
</body>
</html>"""
        
        return html_content