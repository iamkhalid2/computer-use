import cv2
import numpy as np
import pytesseract
from PIL import Image
import json
import logging
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
import torch
import torch.nn as nn
import torchvision.transforms as transforms
from transformers import AutoImageProcessor, AutoModelForObjectDetection

logger = logging.getLogger('ScreenParser')

@dataclass
class UIElement:
    id: int
    type: str  # button, text, link, icon, etc.
    text: str
    bbox: Tuple[int, int, int, int]  # x1, y1, x2, y2
    confidence: float
    description: str
    clickable: bool
    role: Optional[str] = None  # textbox, button, etc.
    semantic_info: Optional[Dict] = None  # Additional semantic information

class ScreenAnalyzer:
    """High-level screen analysis using deep learning models."""
    def __init__(self):
        # Initialize UI element detection model
        self.detector = AutoModelForObjectDetection.from_pretrained("microsoft/OmniParser-v2.0")
        self.processor = AutoImageProcessor.from_pretrained("microsoft/OmniParser-v2.0")
        
        # Initialize text recognition
        if torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")
        self.detector.to(self.device)

    def analyze(self, image: Image.Image) -> List[Dict]:
        """Analyze image and return detected UI elements with semantic information."""
        # Process image through detection model
        inputs = self.processor(images=image, return_tensors="pt").to(self.device)
        outputs = self.detector(**inputs)
        
        # Process detections
        results = []
        for score, label, box in zip(outputs.scores, outputs.labels, outputs.boxes):
            if score > 0.5:  # Confidence threshold
                x1, y1, x2, y2 = box.tolist()
                element = {
                    "type": self.processor.id2label[label.item()],
                    "bbox": (int(x1), int(y1), int(x2), int(y2)),
                    "confidence": float(score),
                    "clickable": True  # Will be refined by detailed analysis
                }
                results.append(element)
        
        return results

class ScreenParser:
    def __init__(self):
        self.elements: List[UIElement] = []
        self.element_counter = 0
        self.analyzer = ScreenAnalyzer()

    def analyze_screen(self, screenshot: Image.Image) -> Dict:
        """Analyze screenshot and return structured information about UI elements."""
        # Convert PIL Image to CV2 format
        cv_image = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        
        # Clear previous elements
        self.elements = []
        self.element_counter = 0

        # 1. Deep Learning Based Analysis
        dl_elements = self.analyzer.analyze(screenshot)
        for elem in dl_elements:
            ui_elem = UIElement(
                id=self.element_counter,
                type=elem["type"],
                text="",  # Will be filled by OCR
                bbox=elem["bbox"],
                confidence=elem["confidence"],
                description=f"Detected {elem['type']}",
                clickable=elem["clickable"]
            )
            self.elements.append(ui_elem)
            self.element_counter += 1

        # 2. OCR Analysis
        self._detect_text(cv_image)
        
        # 3. Semantic Analysis
        self._analyze_semantics()

        # 4. Create screen info string
        screen_info = self._create_screen_info()

        return {
            "elements": self.elements,
            "screen_info": screen_info,
            "element_count": len(self.elements),
            "semantic_structure": self._get_semantic_structure()
        }

    def _detect_text(self, image: np.ndarray):
        """Detect and analyze text in the image."""
        try:
            # Convert to grayscale for better OCR
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            
            try:
                # Get detailed OCR data
                ocr_data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)
            except pytesseract.TesseractNotFoundError:
                logger.error("Tesseract OCR not found. Text detection will be limited.")
                return
            except Exception as e:
                logger.error(f"OCR error: {str(e)}")
                return
            
            # Process OCR results and merge with existing elements
            for i, text in enumerate(ocr_data['text']):
                if not text.strip():
                    continue
                    
                confidence = float(ocr_data['conf'][i])
                if confidence < 60:  # Filter low confidence results
                    continue
                    
                x, y, w, h = (
                    ocr_data['left'][i],
                    ocr_data['top'][i],
                    ocr_data['width'][i],
                    ocr_data['height'][i]
                )
                
                # Try to match with existing elements
                matched = False
                for element in self.elements:
                    if self._boxes_overlap(element.bbox, (x, y, x + w, y + h)):
                        element.text = text
                        element.description = f"{element.type} with text '{text}'"
                        matched = True
                        break
                
                if not matched:
                    # Create new text element
                    element = UIElement(
                        id=self.element_counter,
                        type='text',
                        text=text,
                        bbox=(x, y, x + w, y + h),
                        confidence=confidence,
                        description=f"Text saying '{text}'",
                        clickable=False
                    )
                    self.elements.append(element)
                    self.element_counter += 1

        except Exception as e:
            logger.error(f"Error in text detection: {str(e)}")

    def _analyze_semantics(self):
        """Analyze semantic relationships between elements."""
        for element in self.elements:
            # Add semantic information based on element type and context
            element.semantic_info = {
                "role": self._infer_element_role(element),
                "state": self._infer_element_state(element),
                "relationships": self._find_related_elements(element)
            }

    def _infer_element_role(self, element: UIElement) -> str:
        """Infer the semantic role of an element."""
        if "button" in element.type.lower():
            return "action"
        elif "input" in element.type.lower() or "text" in element.type.lower():
            return "input"
        elif "link" in element.type.lower():
            return "navigation"
        return "static"

    def _infer_element_state(self, element: UIElement) -> Dict:
        """Infer the state of an element (enabled, selected, etc.)."""
        return {
            "enabled": True,  # Default to enabled
            "visible": True,  # Default to visible
            "selected": False  # Default to not selected
        }

    def _find_related_elements(self, element: UIElement) -> List[int]:
        """Find elements that are semantically related to this element."""
        related = []
        for other in self.elements:
            if other.id != element.id:
                # Check for spatial proximity
                if self._are_elements_related(element, other):
                    related.append(other.id)
        return related

    def _are_elements_related(self, elem1: UIElement, elem2: UIElement) -> bool:
        """Determine if two elements are semantically related."""
        # Simple spatial proximity check
        x1, y1 = elem1.bbox[0], elem1.bbox[1]
        x2, y2 = elem2.bbox[0], elem2.bbox[1]
        
        # Consider elements close to each other as related
        distance = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        return distance < 100  # Threshold for relatedness

    def _boxes_overlap(self, box1: Tuple[int, int, int, int], box2: Tuple[int, int, int, int]) -> bool:
        """Check if two bounding boxes overlap."""
        x1_min, y1_min, x1_max, y1_max = box1
        x2_min, y2_min, x2_max, y2_max = box2
        
        return not (x1_max < x2_min or 
                   x2_max < x1_min or 
                   y1_max < y2_min or 
                   y2_max < y1_min)

    def _get_semantic_structure(self) -> Dict:
        """Create a semantic structure of the screen."""
        return {
            "layout": self._analyze_layout(),
            "navigation": self._identify_navigation_elements(),
            "interactive": self._identify_interactive_elements(),
            "content": self._identify_content_areas()
        }

    def _analyze_layout(self) -> Dict:
        """Analyze the overall layout structure."""
        return {
            "regions": self._identify_screen_regions(),
            "hierarchy": self._build_element_hierarchy()
        }

    def _identify_screen_regions(self) -> List[Dict]:
        """Identify main regions of the screen."""
        # Simple implementation - divide screen into regions
        regions = []
        if self.elements:
            min_x = min(e.bbox[0] for e in self.elements)
            max_x = max(e.bbox[2] for e in self.elements)
            min_y = min(e.bbox[1] for e in self.elements)
            max_y = max(e.bbox[3] for e in self.elements)
            
            # Define basic regions
            regions = [
                {"name": "header", "bbox": (min_x, min_y, max_x, min_y + 100)},
                {"name": "main", "bbox": (min_x, min_y + 100, max_x, max_y - 100)},
                {"name": "footer", "bbox": (min_x, max_y - 100, max_x, max_y)}
            ]
        return regions

    def _build_element_hierarchy(self) -> Dict:
        """Build a hierarchical structure of elements."""
        hierarchy = {"root": []}
        for element in self.elements:
            parent = self._find_parent_element(element)
            if parent:
                if parent.id not in hierarchy:
                    hierarchy[parent.id] = []
                hierarchy[parent.id].append(element.id)
            else:
                hierarchy["root"].append(element.id)
        return hierarchy

    def _find_parent_element(self, element: UIElement) -> Optional[UIElement]:
        """Find the parent element that contains this element."""
        candidates = []
        for other in self.elements:
            if other.id != element.id and self._is_contained_within(element.bbox, other.bbox):
                candidates.append(other)
        
        if not candidates:
            return None
            
        # Return the smallest containing element
        return min(candidates, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))

    def _is_contained_within(self, inner_box: Tuple[int, int, int, int], outer_box: Tuple[int, int, int, int]) -> bool:
        """Check if one box is contained within another."""
        return (inner_box[0] >= outer_box[0] and
                inner_box[1] >= outer_box[1] and
                inner_box[2] <= outer_box[2] and
                inner_box[3] <= outer_box[3])

    def _identify_navigation_elements(self) -> List[int]:
        """Identify elements used for navigation."""
        return [e.id for e in self.elements if e.semantic_info["role"] == "navigation"]

    def _identify_interactive_elements(self) -> List[int]:
        """Identify interactive elements."""
        return [e.id for e in self.elements if e.clickable]

    def _identify_content_areas(self) -> List[Dict]:
        """Identify main content areas."""
        content_areas = []
        for element in self.elements:
            if element.type.lower() in ['text', 'paragraph', 'article']:
                content_areas.append({
                    "id": element.id,
                    "type": "content",
                    "bbox": element.bbox
                })
        return content_areas

    def _create_screen_info(self) -> str:
        """Create a structured description of the screen elements."""
        info_parts = []
        
        # Group elements by region
        regions = self._identify_screen_regions()
        for region in regions:
            region_elements = [
                element for element in self.elements
                if self._is_contained_within(element.bbox, region["bbox"])
            ]
            
            if region_elements:
                info_parts.append(f"\n{region['name'].upper()} REGION:")
                for element in region_elements:
                    info = f"ID {element.id}: {element.type} "
                    if element.text:
                        info += f"'{element.text}' "
                    info += f"at {element.bbox}"
                    if element.clickable:
                        info += " (clickable)"
                    if element.semantic_info:
                        info += f" - {element.semantic_info['role']}"
                    info_parts.append(info)
        
        return "\n".join(info_parts)