// Fabric.js Canvas for interactive toolpath editing
let canvas = null;
let shapes = {};  // Store shape objects by name
let shapeData = {};  // Store original mm points and initial positions separately
let gridLines = [];
let workAreaRect = null;

// Undo stack
let undoStack = [];
const MAX_UNDO = 50;

// Toolpath visualization state
let toolpathLocked = false;
let toolpathObjects = [];  // Store toolpath visualization objects (lines, markers, labels)
let toolheadIndicator = null;  // Realtime toolhead position indicator

// Notch tool state
let notchMode = false;
let shapeNotches = {};       // shapeName -> Map<edgeIdx, {edgeIdx, x, y}> of active notches
let notchNodeObjects = [];   // Clickable node circles on canvas (all shapes)
let notchMarkObjects = {};   // shapeName -> [fabric Line objects for V marks]

// Unit display mode
let currentUnit = 'mm';  // 'mm' or 'in'

// Viewport zoom/pan state (separate from the mm→px scale)
let viewZoom = 1;            // Current viewport zoom level
let isPanning = false;       // True while alt+drag pan is active
let lastPanPoint = null;     // Last mouse position during pan

// Canvas dimensions and scale
const WORK_WIDTH = 1720;  // mm
const WORK_HEIGHT = 1660;  // mm
const CANVAS_PADDING = 30;  // px - absolute padding around work area
let scale = 1;
let canvasWidth = 800;
let canvasHeight = 500;

// Ruler state — adjustable right and top boundaries (in mm)
// Must be declared after WORK_WIDTH/WORK_HEIGHT constants above
let rulerRightMm = WORK_WIDTH;    // default = full width
let rulerTopMm   = WORK_HEIGHT;   // default = full height
// Fabric.js ruler objects
let rulerRightLine   = null;
let rulerTopLine     = null;
let rulerRightHandle = null;
let rulerTopHandle   = null;
let rulerBoundsRect  = null;   // invisible rect used for drag-constraint reference
let rulerRightLabel  = null;
let rulerTopLabel    = null;
const RULER_COLOR = '#FFA726';  // amber

// Colors for shapes (brighter for dark mode)
const SHAPE_COLORS = ['#42A5F5', '#66BB6A', '#FFA726', '#EC407A', '#AB47BC', '#26C6DA'];

// Dark mode colors
const BG_COLOR = '#1e1e1e';
const GRID_COLOR = '#333333';
const WORK_AREA_COLOR = '#444444';

function updateCanvasSize() {
    const container = document.getElementById('canvas-container');
    if (!container || !canvas) return;
    
    // Get actual container dimensions
    const newWidth = container.offsetWidth || container.clientWidth || 800;
    const newHeight = container.offsetHeight || container.clientHeight || 520;
    
    // Only resize if dimensions changed significantly
    if (Math.abs(newWidth - canvasWidth) < 5 && Math.abs(newHeight - canvasHeight) < 5) {
        return;
    }
    
    canvasWidth = newWidth;
    canvasHeight = newHeight;
    
    // Ensure minimum size
    if (canvasWidth < 200) canvasWidth = 800;
    if (canvasHeight < 200) canvasHeight = 520;
    
    // Recalculate scale with CANVAS_PADDING on each side
    const availableWidth = canvasWidth - (CANVAS_PADDING * 2);
    const availableHeight = canvasHeight - (CANVAS_PADDING * 2);
    const scaleX = availableWidth / WORK_WIDTH;
    const scaleY = availableHeight / WORK_HEIGHT;
    scale = Math.min(scaleX, scaleY);
    
    // Resize canvas
    canvas.setWidth(canvasWidth);
    canvas.setHeight(canvasHeight);
    
    // Redraw grid and work area
    drawGrid();
    drawWorkArea();
    drawRulers();
    
    // Redraw all shapes at new scale
    redrawAllShapes();
    
    canvas.renderAll();
    console.log('Canvas resized:', canvasWidth, 'x', canvasHeight, 'scale:', scale);
}

function redrawAllShapes() {
    // Re-render all shapes at the new scale
    Object.keys(shapeData).forEach(shapeName => {
        if (shapes[shapeName] && shapeData[shapeName]) {
            const data = shapeData[shapeName];
            const shape = shapes[shapeName];
            
            // Skip if no original points
            if (!data.originalMmPoints) return;
            
            // Remove old shape
            canvas.remove(shape);
            
            // Recreate shape with new scale
            const points = data.originalMmPoints.map(p => ({
                x: toCanvasX(p[0]),
                y: toCanvasY(p[1])
            }));
            
            const newShape = new fabric.Polyline(points, {
                fill: 'transparent',
                stroke: shape.stroke || '#42A5F5',
                strokeWidth: 1,
                selectable: true,
                hasControls: true,
                hasBorders: true,
                lockRotation: true,
                lockScalingX: true,
                lockScalingY: true,
                objectCaching: false,
                shapeName: shapeName
            });
            
            shapes[shapeName] = newShape;
            canvas.add(newShape);
            // Compensate for Fabric.js v5 Polyline strokeWidth/2 bounding box shift
            newShape.set({ left: newShape.left + newShape.strokeWidth / 2, top: newShape.top + newShape.strokeWidth / 2 });
            
            // Update initial position
            data.initialLeft = newShape.left;
            data.initialTop = newShape.top;
        }
    });
    
    // Redraw notch marks at new scale
    redrawAllNotchMarks();
}

function initCanvas(elementId) {
    // Get canvas element and container
    const canvasEl = document.getElementById(elementId);
    if (!canvasEl) {
        console.error('Canvas element not found:', elementId);
        return;
    }
    
    const container = document.getElementById('canvas-container');
    if (!container) {
        console.error('Canvas container not found');
        return;
    }
    
    // Get actual container dimensions - use offsetWidth/Height for rendered size
    canvasWidth = container.offsetWidth || container.clientWidth || 800;
    canvasHeight = container.offsetHeight || container.clientHeight || 520;
    
    // Ensure minimum size
    if (canvasWidth < 200) canvasWidth = 800;
    if (canvasHeight < 200) canvasHeight = 520;
    
    console.log('Container size:', canvasWidth, 'x', canvasHeight);
    
    // Calculate scale to fit work area in canvas with CANVAS_PADDING on each side
    const availableWidth = canvasWidth - (CANVAS_PADDING * 2);
    const availableHeight = canvasHeight - (CANVAS_PADDING * 2);
    const scaleX = availableWidth / WORK_WIDTH;
    const scaleY = availableHeight / WORK_HEIGHT;
    scale = Math.min(scaleX, scaleY);
    
    // Create Fabric canvas with dark mode
    canvas = new fabric.Canvas(elementId, {
        width: canvasWidth,
        height: canvasHeight,
        backgroundColor: BG_COLOR,
        selection: true,
        preserveObjectStacking: true
    });
    
    // Force background color multiple ways (Fabric.js can be stubborn)
    canvas.backgroundColor = BG_COLOR;
    canvas.renderAll();
    
    // Also style the wrapper and lower canvas elements directly
    const wrapper = canvas.wrapperEl;
    if (wrapper) {
        wrapper.style.backgroundColor = BG_COLOR;
    }
    const lowerCanvas = canvas.lowerCanvasEl;
    if (lowerCanvas) {
        lowerCanvas.style.backgroundColor = BG_COLOR;
    }
    
    // Draw grid and work area
    drawGrid();
    drawWorkArea();
    drawRulers();
    
    // Save undo state before any transform starts
    canvas.on('mouse:down', function(e) {
        if (e.target && e.target._isRulerHandle) return;  // no undo save for ruler drags
        if (e.target && (e.target.shapeName || e.target.type === 'activeSelection')) {
            saveUndoState();
        }
        // Initialise notch-mark tracking so the first delta in onShapeMoving is zero
        if (e.target && e.target.shapeName) {
            e.target._notchTrackLeft = e.target.left;
            e.target._notchTrackTop  = e.target.top;
        }
    });

    // Capture start position of a multi-selection before any drag begins
    canvas.on('before:transform', function(e) {
        const obj = e.transform && e.transform.target;
        if (obj && obj.type === 'activeSelection') {
            obj.__startLeft = obj.left;
            obj.__startTop  = obj.top;
        }
    });
    
    // Constrain shapes to work area during drag
    canvas.on('object:moving', onShapeMoving);
    
    // Handle object movement and transforms
    canvas.on('object:moved', function(e) {
        const obj = e.target;
        if (obj && obj._isRulerHandle) return;  // ruler already updated live
        if (obj && obj.type === 'activeSelection') {
            console.log('=== object:moved (activeSelection) ===');
            onGroupMoved(obj);
        } else {
            console.log('=== object:moved EVENT FIRED ===', obj ? obj.shapeName : 'no target');
            onShapeMoved(e);
        }
    });
    canvas.on('object:scaled', onShapeScaled);
    canvas.on('object:rotated', onShapeRotated);
    canvas.on('object:modified', function(e) {
        const obj = e.target;
        if (obj && obj._isRulerHandle) return;  // ruler already updated live
        if (obj && obj.type === 'activeSelection') {
            console.log('=== object:modified (activeSelection) ===');
            onGroupMoved(obj);
        } else {
            console.log('=== object:modified EVENT FIRED ===', obj ? obj.shapeName : 'no target');
            onShapeModified(e);
        }
    });
    
    // Add resize listeners
    window.addEventListener('resize', updateCanvasSize);

    // ── Zoom: scroll wheel zooms toward cursor ──────────────────────────────
    canvas.on('mouse:wheel', function(opt) {
        const delta = opt.e.deltaY;
        let zoom = canvas.getZoom();
        zoom *= 0.999 ** delta;
        zoom = Math.min(Math.max(zoom, 0.5), 20);
        canvas.zoomToPoint({ x: opt.e.offsetX, y: opt.e.offsetY }, zoom);
        viewZoom = zoom;
        opt.e.preventDefault();
        opt.e.stopPropagation();
    });

    // ── Pan: Alt+drag (or middle-mouse drag) pans the viewport ──────────────
    canvas.on('mouse:down', function(opt) {
        if (opt.e.altKey || opt.e.button === 1) {
            isPanning = true;
            lastPanPoint = { x: opt.e.clientX, y: opt.e.clientY };
            canvas.defaultCursor = 'grabbing';
            canvas.discardActiveObject();
        }
    });
    canvas.on('mouse:move', function(opt) {
        if (!isPanning || !lastPanPoint) return;
        const dx = opt.e.clientX - lastPanPoint.x;
        const dy = opt.e.clientY - lastPanPoint.y;
        canvas.relativePan({ x: dx, y: dy });
        lastPanPoint = { x: opt.e.clientX, y: opt.e.clientY };
        canvas.requestRenderAll();
    });
    canvas.on('mouse:up', function(opt) {
        if (isPanning) {
            isPanning = false;
            lastPanPoint = null;
            canvas.defaultCursor = 'default';
        }
    });
    // Prevent page scroll while cursor is over canvas
    canvas.upperCanvasEl.addEventListener('wheel', function(e) { e.preventDefault(); }, { passive: false });
    // ────────────────────────────────────────────────────────────────────────

    // ── Shape name tooltip on hover ─────────────────────────────────────────
    const tooltip = document.createElement('div');
    tooltip.id = 'shape-tooltip';
    tooltip.style.cssText = [
        'position:fixed',
        'background:rgba(0,0,0,0.75)',
        'color:#fff',
        'padding:4px 8px',
        'border-radius:4px',
        'font-size:12px',
        'font-family:monospace',
        'pointer-events:none',
        'display:none',
        'z-index:9999',
        'white-space:nowrap',
    ].join(';');
    document.body.appendChild(tooltip);

    canvas.on('mouse:over', function(opt) {
        const target = opt.target;
        if (target && target.shapeName) {
            tooltip.textContent = target.shapeName;
            tooltip.style.display = 'block';
        }
    });
    canvas.on('mouse:move', function(opt) {
        if (tooltip.style.display === 'block') {
            tooltip.style.left = (opt.e.clientX + 14) + 'px';
            tooltip.style.top  = (opt.e.clientY - 10) + 'px';
        }
    });
    canvas.on('mouse:out', function(opt) {
        tooltip.style.display = 'none';
    });
    // ────────────────────────────────────────────────────────────────────────
    
    // Use ResizeObserver for more reliable container resize detection
    if (typeof ResizeObserver !== 'undefined') {
        const resizeObserver = new ResizeObserver(() => {
            updateCanvasSize();
        });
        resizeObserver.observe(container);
    }
    
    // Also check size after a short delay (for when container is still laying out)
    setTimeout(updateCanvasSize, 100);
    setTimeout(updateCanvasSize, 500);
    
    canvas.renderAll();
    console.log('Canvas initialized:', canvasWidth, 'x', canvasHeight, 'scale:', scale);
}

function toCanvasX(mmX) {
    // Convert mm to canvas coordinates with fixed padding
    const workAreaWidth = WORK_WIDTH * scale;
    const offsetX = CANVAS_PADDING + (canvasWidth - CANVAS_PADDING * 2 - workAreaWidth) / 2;
    return mmX * scale + offsetX;
}

function toCanvasY(mmY) {
    // Convert mm to canvas coordinates (Y is flipped) with fixed padding
    const workAreaHeight = WORK_HEIGHT * scale;
    const offsetY = CANVAS_PADDING + (canvasHeight - CANVAS_PADDING * 2 - workAreaHeight) / 2;
    return canvasHeight - (mmY * scale + offsetY);
}

function fromCanvasX(canvasX) {
    const workAreaWidth = WORK_WIDTH * scale;
    const offsetX = CANVAS_PADDING + (canvasWidth - CANVAS_PADDING * 2 - workAreaWidth) / 2;
    return (canvasX - offsetX) / scale;
}

function fromCanvasY(canvasY) {
    const workAreaHeight = WORK_HEIGHT * scale;
    const offsetY = CANVAS_PADDING + (canvasHeight - CANVAS_PADDING * 2 - workAreaHeight) / 2;
    return (canvasHeight - canvasY - offsetY) / scale;
}

let axisLabels = [];  // Store axis label objects

function drawGrid() {
    // Remove old grid lines and labels
    gridLines.forEach(line => canvas.remove(line));
    gridLines = [];
    axisLabels.forEach(label => canvas.remove(label));
    axisLabels = [];
    
    // Grid spacing: 20mm uniform grid
    // Labels every 200mm (mm mode) or every 5 inches / 127mm (inch mode)
    const gridSpacing = 20;   // mm - uniform grid lines
    const useInches = (currentUnit === 'in');
    const labelSpacing = useInches ? 127 : 200; // mm
    
    const gridColor = '#2a2a2a';
    const labelColor = '#888888';
    
    // Draw vertical grid lines
    for (let x = 0; x <= WORK_WIDTH; x += gridSpacing) {
        const line = new fabric.Line([toCanvasX(x), toCanvasY(0), toCanvasX(x), toCanvasY(WORK_HEIGHT)], {
            stroke: gridColor,
            strokeWidth: 1,
            selectable: false,
            evented: false
        });
        gridLines.push(line);
        canvas.add(line);
    }
    
    // Draw horizontal grid lines
    for (let y = 0; y <= WORK_HEIGHT; y += gridSpacing) {
        const line = new fabric.Line([toCanvasX(0), toCanvasY(y), toCanvasX(WORK_WIDTH), toCanvasY(y)], {
            stroke: gridColor,
            strokeWidth: 1,
            selectable: false,
            evented: false
        });
        gridLines.push(line);
        canvas.add(line);
    }
    
    // X axis labels
    for (let x = 0; x <= WORK_WIDTH; x += labelSpacing) {
        const labelText = useInches ? Math.round(x / 25.4) + '"' : Math.round(x).toString();
        const label = new fabric.Text(labelText, {
            left: toCanvasX(x),
            top: toCanvasY(0) + 5,
            fontSize: 10,
            fontFamily: 'Roboto, sans-serif',
            fill: labelColor,
            selectable: false,
            evented: false,
            originX: 'center'
        });
        axisLabels.push(label);
        canvas.add(label);
    }
    
    // Y axis labels
    for (let y = 0; y <= WORK_HEIGHT; y += labelSpacing) {
        const labelText = useInches ? Math.round(y / 25.4) + '"' : Math.round(y).toString();
        const label = new fabric.Text(labelText, {
            left: toCanvasX(0) - 5,
            top: toCanvasY(y),
            fontSize: 10,
            fontFamily: 'Roboto, sans-serif',
            fill: labelColor,
            selectable: false,
            evented: false,
            originX: 'right',
            originY: 'center'
        });
        axisLabels.push(label);
        canvas.add(label);
    }
    
    // Send grid and labels to back
    gridLines.forEach(line => canvas.sendToBack(line));
}

function drawWorkArea() {
    if (workAreaRect) {
        canvas.remove(workAreaRect);
    }
    
    workAreaRect = new fabric.Rect({
        left: toCanvasX(0),
        top: toCanvasY(WORK_HEIGHT),
        width: WORK_WIDTH * scale,
        height: WORK_HEIGHT * scale,
        fill: 'transparent',
        stroke: WORK_AREA_COLOR,
        strokeWidth: 2,
        selectable: false,
        evented: false
    });
    canvas.add(workAreaRect);
    canvas.sendToBack(workAreaRect);
}

// Return the current ruler-bounded work area (mm)
function getRulerBounds() {
    return { right: rulerRightMm, top: rulerTopMm };
}

// Bring ruler handles / labels to the very front so they're always clickable
function ensureRulerHandlesFront() {
    if (rulerRightHandle) canvas.bringToFront(rulerRightHandle);
    if (rulerTopHandle)   canvas.bringToFront(rulerTopHandle);
    if (rulerRightLabel)  canvas.bringToFront(rulerRightLabel);
    if (rulerTopLabel)    canvas.bringToFront(rulerTopLabel);
}

// Draw (or redraw) ruler lines, handles, and the invisible bounds rect
function drawRulers() {
    // Remove existing ruler objects
    [rulerRightLine, rulerTopLine, rulerRightHandle, rulerTopHandle,
     rulerBoundsRect, rulerRightLabel, rulerTopLabel].forEach(obj => {
        if (obj) canvas.remove(obj);
    });
    rulerRightLine = rulerTopLine = rulerRightHandle = rulerTopHandle = null;
    rulerBoundsRect = rulerRightLabel = rulerTopLabel = null;

    const rx        = toCanvasX(rulerRightMm);
    const ty        = toCanvasY(rulerTopMm);
    const workLeft  = toCanvasX(0);
    const workRight = toCanvasX(WORK_WIDTH);
    const workTop   = toCanvasY(WORK_HEIGHT);   // top of the work area in canvas Y
    const workBottom = toCanvasY(0);            // bottom of the work area in canvas Y

    // --- right ruler: dashed vertical line, clipped to work area height ---
    rulerRightLine = new fabric.Line([rx, workTop, rx, workBottom], {
        stroke: RULER_COLOR, strokeWidth: 1.5,
        strokeDashArray: [6, 4],
        selectable: false, evented: false,
        opacity: 0.85,
        _isRulerLine: true
    });

    // --- top ruler: dashed horizontal line, clipped to work area width ---
    rulerTopLine = new fabric.Line([workLeft, ty, workRight, ty], {
        stroke: RULER_COLOR, strokeWidth: 1.5,
        strokeDashArray: [6, 4],
        selectable: false, evented: false,
        opacity: 0.85,
        _isRulerLine: true
    });

    // --- right ruler handle: triangle tab at the top of the ruler line ---
    // Triangle sits just above the work area top edge; points downward (into work area).
    rulerRightHandle = new fabric.Triangle({
        left: rx, top: workTop - 8,
        width: 14, height: 12, angle: 180,
        fill: RULER_COLOR, stroke: '#fff', strokeWidth: 1,
        originX: 'center', originY: 'center',
        hasControls: false, hasBorders: false,
        lockMovementY: true,
        selectable: true, evented: true,
        hoverCursor: 'ew-resize',
        _isRulerHandle: 'right'
    });

    // --- top ruler handle: triangle tab to the LEFT of the left axis (in padding) ---
    // Triangle sits outside the work area; points rightward (into work area).
    rulerTopHandle = new fabric.Triangle({
        left: workLeft - 10, top: ty,
        width: 12, height: 14, angle: 90,
        fill: RULER_COLOR, stroke: '#fff', strokeWidth: 1,
        originX: 'center', originY: 'center',
        hasControls: false, hasBorders: false,
        lockMovementX: true,
        selectable: true, evented: true,
        hoverCursor: 'ns-resize',
        _isRulerHandle: 'top'
    });

    // --- labels ---
    const useInches = (currentUnit === 'in');
    const rightText = useInches
        ? (rulerRightMm / 25.4).toFixed(1) + '"'
        : Math.round(rulerRightMm) + 'mm';
    const topText = useInches
        ? (rulerTopMm / 25.4).toFixed(1) + '"'
        : Math.round(rulerTopMm) + 'mm';

    rulerRightLabel = new fabric.Text(rightText, {
        left: rx + 5, top: workTop - 22,
        fontSize: 10, fill: RULER_COLOR,
        fontFamily: 'Roboto, sans-serif',
        selectable: false, evented: false,
        _isRulerLabel: true
    });

    rulerTopLabel = new fabric.Text(topText, {
        left: workLeft - 8, top: ty - 15,
        originX: 'right',
        fontSize: 10, fill: RULER_COLOR,
        fontFamily: 'Roboto, sans-serif',
        selectable: false, evented: false,
        _isRulerLabel: true
    });

    // --- invisible bounds rect for onShapeMoving constraint ---
    rulerBoundsRect = new fabric.Rect({
        left: toCanvasX(0),
        top:  toCanvasY(rulerTopMm),
        width:  rulerRightMm * scale,
        height: rulerTopMm   * scale,
        fill: 'transparent', stroke: 'transparent', strokeWidth: 0,
        selectable: false, evented: false,
        _isRulerBoundsRect: true
    });

    canvas.add(rulerBoundsRect);
    canvas.add(rulerRightLine);
    canvas.add(rulerTopLine);
    canvas.add(rulerRightHandle);
    canvas.add(rulerTopHandle);
    canvas.add(rulerRightLabel);
    canvas.add(rulerTopLabel);

    canvas.sendToBack(rulerBoundsRect);
    ensureRulerHandlesFront();
}

// Called from object:moving when the moving object is a ruler handle
function onRulerHandleMoving(obj) {
    if (obj._isRulerHandle === 'right') {
        // Clamp handle X to [left axis … right axis]
        const minX = toCanvasX(0);
        const maxX = toCanvasX(WORK_WIDTH);
        obj.left = Math.max(minX, Math.min(maxX, obj.left));

        rulerRightMm = Math.max(0, Math.min(WORK_WIDTH, fromCanvasX(obj.left)));

        if (rulerRightLine) {
            const wTop    = toCanvasY(WORK_HEIGHT);
            const wBottom = toCanvasY(0);
            rulerRightLine.set({ x1: obj.left, x2: obj.left, y1: wTop, y2: wBottom });
            rulerRightLine.setCoords();
        }
        if (rulerRightLabel) {
            const txt = currentUnit === 'in'
                ? (rulerRightMm / 25.4).toFixed(1) + '"'
                : Math.round(rulerRightMm) + 'mm';
            rulerRightLabel.set({ text: txt, left: obj.left + 5 });
        }
        if (rulerBoundsRect) {
            rulerBoundsRect.set({ width: rulerRightMm * scale });
            rulerBoundsRect.setCoords();
        }

    } else if (obj._isRulerHandle === 'top') {
        // Clamp handle Y to [work area top … work area bottom]
        const minY = toCanvasY(WORK_HEIGHT);   // smaller canvas Y = higher mm
        const maxY = toCanvasY(0);
        obj.top = Math.max(minY, Math.min(maxY, obj.top));

        rulerTopMm = Math.max(0, Math.min(WORK_HEIGHT, fromCanvasY(obj.top)));

        if (rulerTopLine) {
            const wLeft  = toCanvasX(0);
            const wRight = toCanvasX(WORK_WIDTH);
            rulerTopLine.set({ y1: obj.top, y2: obj.top, x1: wLeft, x2: wRight });
            rulerTopLine.setCoords();
        }
        if (rulerTopLabel) {
            const txt = currentUnit === 'in'
                ? (rulerTopMm / 25.4).toFixed(1) + '"'
                : Math.round(rulerTopMm) + 'mm';
            rulerTopLabel.set({ text: txt, top: obj.top - 15, originX: 'right' });
        }
        if (rulerBoundsRect) {
            rulerBoundsRect.set({
                top:    toCanvasY(rulerTopMm),
                height: rulerTopMm * scale
            });
            rulerBoundsRect.setCoords();
        }
    }
}

// Helper: Constrain points to ruler-bounded work area
function constrainShapeToWorkArea(points) {
    const boundRight = rulerRightMm;
    const boundTop   = rulerTopMm;

    const xVals = points.map(p => p[0]);
    const yVals = points.map(p => p[1]);
    const minX = Math.min(...xVals);
    const maxX = Math.max(...xVals);
    const minY = Math.min(...yVals);
    const maxY = Math.max(...yVals);

    let offsetX = 0, offsetY = 0;
    if (minX < 0) offsetX = -minX;
    else if (maxX > boundRight) offsetX = boundRight - maxX;
    if (minY < 0) offsetY = -minY;
    else if (maxY > boundTop) offsetY = boundTop - maxY;

    if (offsetX !== 0 || offsetY !== 0) {
        return points.map(p => [p[0] + offsetX, p[1] + offsetY]);
    }
    return points;
}

function clearShapes() {
    // Remove all shape objects from canvas
    Object.values(shapes).forEach(shape => {
        canvas.remove(shape);
    });
    shapes = {};
    shapeData = {};  // Clear stored data too
    undoStack = [];  // Clear undo history
    clipboard = null;  // Clear clipboard
    
    // Exit notch mode and clear all notch data
    if (notchMode) disableNotchMode();
    shapeNotches = {};
    notchNodeObjects.forEach(obj => canvas.remove(obj));
    notchNodeObjects = [];
    Object.values(notchMarkObjects).forEach(arr => arr.forEach(obj => canvas.remove(obj)));
    notchMarkObjects = {};
    // Also remove any other objects that aren't grid/work area/rulers
    const objectsToRemove = canvas.getObjects().filter(obj => 
        obj !== workAreaRect &&
        !gridLines.includes(obj) &&
        !axisLabels.includes(obj) &&
        !obj._isRulerLine &&
        !obj._isRulerHandle &&
        !obj._isRulerLabel &&
        !obj._isRulerBoundsRect
    );
    objectsToRemove.forEach(obj => canvas.remove(obj));
    
    canvas.discardActiveObject();
    canvas.renderAll();
    console.log('Canvas cleared');
}

function addShape(name, points, colorIndexOrColor, segmentBreaks, segmentTypes) {
    if (!canvas || !points || points.length < 2) return;
    
    // If shape with this name already exists, generate a unique name
    let uniqueName = name;
    let counter = 1;
    while (shapes[uniqueName]) {
        uniqueName = name + '_' + counter;
        counter++;
    }
    name = uniqueName;
    
    // Log what we received
    const xVals = points.map(p => p[0]);
    const yVals = points.map(p => p[1]);
    const minX = Math.min(...xVals);
    const maxX = Math.max(...xVals);
    const minY = Math.min(...yVals);
    const maxY = Math.max(...yVals);
    const shapeWidth = maxX - minX;
    const shapeHeight = maxY - minY;
    
    console.log('addShape:', name, 
        'mm X(' + minX.toFixed(1) + '-' + maxX.toFixed(1) + ')',
        'Y(' + minY.toFixed(1) + '-' + maxY.toFixed(1) + ')',
        'size: ' + shapeWidth.toFixed(1) + ' x ' + shapeHeight.toFixed(1));
    
    // Check if shape can fit in work area at all
    if (shapeWidth > WORK_WIDTH || shapeHeight > WORK_HEIGHT) {
        const msg = `Shape "${name}" (${shapeWidth.toFixed(0)}mm x ${shapeHeight.toFixed(0)}mm) is too large for work area (${WORK_WIDTH}mm x ${WORK_HEIGHT}mm)`;
        console.error(msg);
        throw new Error(msg);
    }
    
    // Store original mm points in separate object (deep copy)
    shapeData[name] = {
        originalMmPoints: points.map(p => [p[0], p[1]]),
        segmentBreaks: (segmentBreaks && segmentBreaks.length > 0) ? segmentBreaks.slice() : [0],
        segmentTypes: (segmentTypes && segmentTypes.length > 0) ? segmentTypes.slice() : [],
        initialLeft: null,
        initialTop: null
    };
    
    // Constrain shape to ruler-bounded area if needed
    if (minX < 0 || maxX > rulerRightMm || minY < 0 || maxY > rulerTopMm) {
        let offsetX = 0, offsetY = 0;
        if (minX < 0) offsetX = -minX;
        else if (maxX > rulerRightMm) offsetX = rulerRightMm - maxX;
        if (minY < 0) offsetY = -minY;
        else if (maxY > rulerTopMm) offsetY = rulerTopMm - maxY;
        
        // Apply offset to constrain within bounds
        shapeData[name].originalMmPoints = points.map(p => [p[0] + offsetX, p[1] + offsetY]);
        points = shapeData[name].originalMmPoints;
        console.log('Shape constrained to ruler area:', name);
    }
    
    // Convert points to canvas coordinates
    const canvasPoints = points.map(p => ({
        x: toCanvasX(p[0]),
        y: toCanvasY(p[1])
    }));
    
    // Create polyline path - colorIndexOrColor can be a number (index) or string (color)
    const color = typeof colorIndexOrColor === 'string' 
        ? colorIndexOrColor 
        : SHAPE_COLORS[(colorIndexOrColor || 0) % SHAPE_COLORS.length];
    
    const polyline = new fabric.Polyline(canvasPoints, {
        fill: 'transparent',
        stroke: color,
        strokeWidth: 1,
        selectable: true,
        hasControls: true,
        hasBorders: true,
        lockRotation: false,
        lockScalingX: false,
        lockScalingY: false,
        cornerColor: color,
        borderColor: color,
        cornerSize: 10,
        cornerStyle: 'circle',
        transparentCorners: false,
        objectCaching: false,
        shapeName: name
    });
    
    shapes[name] = polyline;
    canvas.add(polyline);
    canvas.bringToFront(polyline);
    // Compensate for Fabric.js v5 Polyline strokeWidth/2 bounding box shift
    polyline.set({ left: polyline.left + polyline.strokeWidth / 2, top: polyline.top + polyline.strokeWidth / 2 });
    canvas.setActiveObject(polyline);
    canvas.renderAll();
    
    // Keep ruler handles on top
    ensureRulerHandlesFront();
    
    // Store initial left/top position AFTER adding to canvas
    shapeData[name].initialLeft = polyline.left;
    shapeData[name].initialTop = polyline.top;

    // If notch mode is active, show node circles for the newly added shape
    if (notchMode) showNotchNodes();

    console.log('addShape stored:', name,
        'initialLeft:', polyline.left.toFixed(1),
        'initialTop:', polyline.top.toFixed(1));
}

// Constrain shape to work area during drag (real-time)
function onShapeMoving(e) {
    const obj = e.target;
    if (!obj) return;

    // Ruler handle — delegate to dedicated handler
    if (obj._isRulerHandle) {
        onRulerHandleMoving(obj);
        return;
    }

    if (!obj.shapeName) return;
    
    // Use the ruler bounds rect (tracks current ruler area); fall back to full work area
    const boundsRect = rulerBoundsRect || workAreaRect;
    if (!boundsRect) return;
    
    // Get the bounding box of the shape in canvas-coordinate space (not viewport space)
    // so it stays consistent with obj.left/obj.top regardless of viewport zoom level.
    const bound = obj.getBoundingRect(false, true);
    const work = boundsRect.getBoundingRect(false, true);
    
    // Calculate offset between object origin (left,top) and bounding rect
    const offsetLeft = obj.left - bound.left;
    const offsetTop = obj.top - bound.top;
    
    // Calculate the allowed range for the bounding rect
    const minBoundLeft = work.left;
    const maxBoundLeft = work.left + work.width - bound.width;
    const minBoundTop = work.top;
    const maxBoundTop = work.top + work.height - bound.height;
    
    // Constrain the bounding rect position
    let newBoundLeft = Math.max(minBoundLeft, Math.min(maxBoundLeft, bound.left));
    let newBoundTop = Math.max(minBoundTop, Math.min(maxBoundTop, bound.top));
    
    // Convert back to object origin position
    obj.left = newBoundLeft + offsetLeft;
    obj.top = newBoundTop + offsetTop;

    // Translate notch marks in real-time so they follow the dragged shape
    const prevLeft = obj._notchTrackLeft !== undefined ? obj._notchTrackLeft : obj.left;
    const prevTop  = obj._notchTrackTop  !== undefined ? obj._notchTrackTop  : obj.top;
    const dLeft = obj.left - prevLeft;
    const dTop  = obj.top  - prevTop;
    obj._notchTrackLeft = obj.left;
    obj._notchTrackTop  = obj.top;
    if ((dLeft !== 0 || dTop !== 0) && notchMarkObjects[obj.shapeName]) {
        notchMarkObjects[obj.shapeName].forEach(line => {
            line.left += dLeft;
            line.top  += dTop;
            line.setCoords();
        });
    }
}

// Get the current mm points for a shape based on its canvas position
function getCurrentMmPoints(shape) {
    if (!shape || !shape.shapeName) return null;
    
    const data = shapeData[shape.shapeName];
    if (!data || !data.originalMmPoints || data.initialLeft === null) return null;
    
    // Calculate how much the object moved in canvas pixels from initial position
    const deltaCanvasX = shape.left - data.initialLeft;
    const deltaCanvasY = shape.top - data.initialTop;
    
    // Convert canvas delta to mm delta
    const deltaMmX = deltaCanvasX / scale;
    const deltaMmY = -deltaCanvasY / scale;  // Y is flipped
    
    console.log('getCurrentMmPoints:', shape.shapeName, 
        'shape.left:', shape.left, 'initialLeft:', data.initialLeft,
        'deltaCanvas:', deltaCanvasX, deltaCanvasY,
        'deltaMm:', deltaMmX, deltaMmY);
    
    // Apply delta to stored mm points
    return data.originalMmPoints.map(p => [
        p[0] + deltaMmX,
        p[1] + deltaMmY
    ]);
}

// Handle a multi-select (activeSelection) drag — apply group delta to every shape
function onGroupMoved(group) {
    if (!group || group.type !== 'activeSelection') return;

    const startLeft = group.__startLeft !== undefined ? group.__startLeft : group.left;
    const startTop  = group.__startTop  !== undefined ? group.__startTop  : group.top;

    const deltaCanvasX = group.left - startLeft;
    const deltaCanvasY = group.top  - startTop;

    // Clean up stored start coords
    delete group.__startLeft;
    delete group.__startTop;

    if (Math.abs(deltaCanvasX) < 2 && Math.abs(deltaCanvasY) < 2) {
        console.log('onGroupMoved: ignoring tiny delta', deltaCanvasX, deltaCanvasY);
        return;
    }

    const deltaMmX =  deltaCanvasX / scale;
    const deltaMmY = -deltaCanvasY / scale;  // canvas Y is flipped

    console.log('onGroupMoved: deltaCanvas', deltaCanvasX.toFixed(1), deltaCanvasY.toFixed(1),
        'deltaMm', deltaMmX.toFixed(1), deltaMmY.toFixed(1));

    group.getObjects().forEach(subObj => {
        const name = subObj.shapeName;
        if (!name) return;

        const data = shapeData[name];
        if (!data || !data.originalMmPoints) return;

        let newPoints = data.originalMmPoints.map(p => [p[0] + deltaMmX, p[1] + deltaMmY]);

        // Constrain to ruler-bounded work area
        const xs = newPoints.map(p => p[0]);
        const ys = newPoints.map(p => p[1]);
        let cx = 0, cy = 0;
        if (Math.min(...xs) < 0)               cx = -Math.min(...xs);
        else if (Math.max(...xs) > rulerRightMm)  cx = rulerRightMm - Math.max(...xs);
        if (Math.min(...ys) < 0)               cy = -Math.min(...ys);
        else if (Math.max(...ys) > rulerTopMm) cy = rulerTopMm - Math.max(...ys);
        if (cx !== 0 || cy !== 0) {
            newPoints = newPoints.map(p => [p[0] + cx, p[1] + cy]);
        }

        data.originalMmPoints = newPoints;
        // Update notch nodeKey mm positions so computeNotchGeometry uses the new location
        if (shapeNotches[name]) {
            shapeNotches[name].forEach((nodeKey) => {
                nodeKey.x += deltaMmX + cx;
                nodeKey.y += deltaMmY + cy;
            });
        }
        // initialLeft/Top will be refreshed by redrawShapeFromData below
        emitShapeUpdate(name);
        console.log('  updated', name, 'mm X(' +
            Math.min(...newPoints.map(p=>p[0])).toFixed(1) + '-' +
            Math.max(...newPoints.map(p=>p[0])).toFixed(1) + ')'
        );
    });

    // Redraw each shape so initialLeft/Top are reset to their true canvas positions
    group.getObjects().forEach(subObj => {
        if (subObj.shapeName) redrawShapeFromData(subObj.shapeName);
    });

    // Redraw notch marks so they follow each moved shape
    group.getObjects().forEach(subObj => {
        if (subObj.shapeName) drawNotchMarksForShape(subObj.shapeName);
    });
}

function onShapeMoved(e) {
    const obj = e.target;
    console.log('=== onShapeMoved TRIGGERED ===', obj ? obj.shapeName : 'no object');
    if (!obj || !obj.shapeName) return;
    
    const name = obj.shapeName;
    const data = shapeData[name];
    
    if (!data || !data.originalMmPoints || data.initialLeft === null) {
        console.error('No stored data for shape:', name);
        return;
    }
    
    // Calculate how much the object moved in canvas pixels
    const deltaCanvasX = obj.left - data.initialLeft;
    const deltaCanvasY = obj.top - data.initialTop;
    
    // Ignore tiny movements (like initial render noise)
    if (Math.abs(deltaCanvasX) < 2 && Math.abs(deltaCanvasY) < 2) {
        console.log('Ignoring tiny movement for', name);
        return;
    }
    
    // Convert canvas delta to mm delta
    // X: just divide by scale
    // Y: divide by scale and negate (canvas Y is flipped)
    const deltaMmX = deltaCanvasX / scale;
    const deltaMmY = -deltaCanvasY / scale;
    
    console.log('onShapeMoved:', name,
        'canvas delta:', deltaCanvasX.toFixed(1), deltaCanvasY.toFixed(1),
        'mm delta:', deltaMmX.toFixed(1), deltaMmY.toFixed(1));
    
    // Apply delta to original mm points
    let newPoints = data.originalMmPoints.map(p => [
        p[0] + deltaMmX,
        p[1] + deltaMmY
    ]);
    
    // Check bounds and constrain if necessary
    let minX = Math.min(...newPoints.map(p => p[0]));
    let maxX = Math.max(...newPoints.map(p => p[0]));
    let minY = Math.min(...newPoints.map(p => p[1]));
    let maxY = Math.max(...newPoints.map(p => p[1]));
    
    // Constrain to work area
    let constrainX = 0, constrainY = 0;
    if (minX < 0) constrainX = -minX;
    else if (maxX > rulerRightMm) constrainX = rulerRightMm - maxX;
    if (minY < 0) constrainY = -minY;
    else if (maxY > rulerTopMm) constrainY = rulerTopMm - maxY;
    
    if (constrainX !== 0 || constrainY !== 0) {
        // Apply constraint
        newPoints = newPoints.map(p => [p[0] + constrainX, p[1] + constrainY]);
        
        // Recalculate canvas position for constrained shape
        const constrainedCanvasPoints = newPoints.map(p => ({
            x: toCanvasX(p[0]),
            y: toCanvasY(p[1])
        }));
        
        // Find the new bounding box top-left
        const canvasMinX = Math.min(...constrainedCanvasPoints.map(p => p.x));
        const canvasMinY = Math.min(...constrainedCanvasPoints.map(p => p.y));
        
        // Update object position
        obj.left = canvasMinX;
        obj.top = canvasMinY;
        obj.setCoords();
        canvas.renderAll();
        
        console.log('Shape constrained to work area:', name);
    }
    
    // Update stored values for next move (deep copy)
    data.originalMmPoints = newPoints.map(p => [p[0], p[1]]);
    data.initialLeft = obj.left;
    data.initialTop = obj.top;

    // Update notch nodeKey mm positions so computeNotchGeometry uses the new location
    const effectiveDeltaX = deltaMmX + constrainX;
    const effectiveDeltaY = deltaMmY + constrainY;
    if (shapeNotches[name]) {
        shapeNotches[name].forEach((nodeKey) => {
            nodeKey.x += effectiveDeltaX;
            nodeKey.y += effectiveDeltaY;
        });
    }

    // Log result
    const newX = newPoints.map(p => p[0]);
    const newY = newPoints.map(p => p[1]);
    console.log('Shape moved:', name, 
        'new mm X(' + Math.min(...newX).toFixed(1) + '-' + Math.max(...newX).toFixed(1) + ')',
        'Y(' + Math.min(...newY).toFixed(1) + '-' + Math.max(...newY).toFixed(1) + ')');
    
    // Redraw notch marks so they follow the shape
    drawNotchMarksForShape(name);
    if (notchMode) showNotchNodes();

    // Send update to Python backend
    if (window.emitEvent) {
        window.emitEvent('shape_moved', {
            shapeName: name,
            newPoints: newPoints
        });
    }
}

// Handle shape scaling via handles
function onShapeScaled(e) {
    const obj = e.target;
    if (!obj || !obj.shapeName) return;
    
    const name = obj.shapeName;
    const data = shapeData[name];
    if (!data || !data.originalMmPoints) return;
    
    const scaleX = obj.scaleX;
    const scaleY = obj.scaleY;
    
    // Get object center in canvas coords
    const centerCanvas = obj.getCenterPoint();
    const centerMmX = fromCanvasX(centerCanvas.x);
    const centerMmY = fromCanvasY(centerCanvas.y);
    
    // Scale points around the center
    const originalPoints = data.originalMmPoints;
    const oldXVals = originalPoints.map(p => p[0]);
    const oldYVals = originalPoints.map(p => p[1]);
    const oldCenterX = (Math.min(...oldXVals) + Math.max(...oldXVals)) / 2;
    const oldCenterY = (Math.min(...oldYVals) + Math.max(...oldYVals)) / 2;
    
    let newPoints = originalPoints.map(p => [
        centerMmX + (p[0] - oldCenterX) * scaleX,
        centerMmY + (p[1] - oldCenterY) * scaleY
    ]);
    
    // Constrain to work area bounds
    newPoints = constrainShapeToWorkArea(newPoints);
    
    // Reset object scale and update points
    obj.scaleX = 1;
    obj.scaleY = 1;
    
    data.originalMmPoints = newPoints;
    redrawShapeFromData(name);
    emitShapeUpdate(name);
    
    console.log('Shape scaled:', name, 'scaleX:', scaleX.toFixed(2), 'scaleY:', scaleY.toFixed(2));
}

// Handle shape rotation via handles
function onShapeRotated(e) {
    const obj = e.target;
    if (!obj || !obj.shapeName) return;
    
    const name = obj.shapeName;
    const data = shapeData[name];
    if (!data || !data.originalMmPoints) return;
    
    // Negate angle because canvas Y is flipped
    const angle = -obj.angle * Math.PI / 180; // Convert to radians, negate for flipped Y
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    
    // Get object center in mm coords
    const centerCanvas = obj.getCenterPoint();
    const centerMmX = fromCanvasX(centerCanvas.x);
    const centerMmY = fromCanvasY(centerCanvas.y);
    
    // Rotate points around center
    const originalPoints = data.originalMmPoints;
    const oldXVals = originalPoints.map(p => p[0]);
    const oldYVals = originalPoints.map(p => p[1]);
    const oldCenterX = (Math.min(...oldXVals) + Math.max(...oldXVals)) / 2;
    const oldCenterY = (Math.min(...oldYVals) + Math.max(...oldYVals)) / 2;
    
    let newPoints = originalPoints.map(p => {
        const dx = p[0] - oldCenterX;
        const dy = p[1] - oldCenterY;
        return [
            centerMmX + dx * cos - dy * sin,
            centerMmY + dx * sin + dy * cos
        ];
    });
    
    // Constrain to work area bounds
    newPoints = constrainShapeToWorkArea(newPoints);
    
    // Reset object rotation and update points
    obj.angle = 0;
    
    data.originalMmPoints = newPoints;
    redrawShapeFromData(name);
    emitShapeUpdate(name);
    
    console.log('Shape rotated:', name, 'angle:', (-angle * 180 / Math.PI).toFixed(1) + '°');
}

// Handle any modification (fallback)
function onShapeModified(e) {
    const obj = e.target;
    if (!obj || !obj.shapeName) return;
    
    const name = obj.shapeName;
    const data = shapeData[name];
    
    // Always check for position change first
    if (data && data.initialLeft !== null) {
        const deltaCanvasX = obj.left - data.initialLeft;
        const deltaCanvasY = obj.top - data.initialTop;
        
        // If there's a significant position change, treat as move
        if (Math.abs(deltaCanvasX) > 2 || Math.abs(deltaCanvasY) > 2) {
            console.log('onShapeModified detected move:', name, 'delta:', deltaCanvasX.toFixed(1), deltaCanvasY.toFixed(1));
            onShapeMoved(e);
            return;  // onShapeMoved handles everything
        }
    }
    
    // Reset any remaining transforms
    if (obj.scaleX !== 1 || obj.scaleY !== 1) {
        onShapeScaled(e);
    }
    if (obj.angle !== 0) {
        onShapeRotated(e);
    }
}

function getShapePositions() {
    // Return current positions of all shapes in mm
    const positions = {};
    
    console.log('=== getShapePositions called ===');
    Object.keys(shapeData).forEach(name => {
        const data = shapeData[name];
        if (data && data.originalMmPoints) {
            const pts = data.originalMmPoints;
            const minX = Math.min(...pts.map(p => p[0]));
            const maxX = Math.max(...pts.map(p => p[0]));
            const minY = Math.min(...pts.map(p => p[1]));
            const maxY = Math.max(...pts.map(p => p[1]));
            console.log(`  ${name}: X(${minX.toFixed(1)}-${maxX.toFixed(1)}) Y(${minY.toFixed(1)}-${maxY.toFixed(1)})`);
            positions[name] = data.originalMmPoints.map(p => [p[0], p[1]]);
        }
    });
    
    return positions;
}

// Resize handler
function resizeCanvas() {
    if (!canvas) return;
    
    const container = document.getElementById('canvas-container');
    if (!container) return;
    
    canvasWidth = container.clientWidth || 800;
    canvasHeight = container.clientHeight || 500;
    
    // Ensure minimum size
    if (canvasWidth < 100) canvasWidth = 800;
    if (canvasHeight < 100) canvasHeight = 500;
    
    const scaleX = canvasWidth / WORK_WIDTH;
    const scaleY = canvasHeight / WORK_HEIGHT;
    scale = Math.min(scaleX, scaleY) * 0.95;
    
    canvas.setWidth(canvasWidth);
    canvas.setHeight(canvasHeight);
    canvas.setBackgroundColor(BG_COLOR, canvas.renderAll.bind(canvas));
    
    // Redraw grid and work area
    drawGrid();
    drawWorkArea();

    // TODO: Rescale existing shapes
    canvas.renderAll();
}

// ============ SHAPE MANIPULATION TOOLS ============

// Get selected shape (single)
function getSelectedShape() {
    const activeObj = canvas.getActiveObject();
    if (activeObj && activeObj.shapeName) {
        return activeObj;
    }
    return null;
}

// Get all selected shapes (handles multi-select)
function getSelectedShapes() {
    const activeObj = canvas.getActiveObject();
    if (!activeObj) return [];
    
    // If it's an ActiveSelection (multi-select), get all objects
    if (activeObj.type === 'activeSelection') {
        return activeObj.getObjects().filter(obj => obj.shapeName);
    }
    
    // Single selection
    if (activeObj.shapeName) {
        return [activeObj];
    }
    
    return [];
}

// Mirror shape(s) horizontally (X axis) - supports multi-select
function mirrorX() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        
        // Get current mm points based on canvas position
        const points = getCurrentMmPoints(shape);
        if (!points) return;
        
        // Find center X of shape
        const xVals = points.map(p => p[0]);
        const centerX = (Math.min(...xVals) + Math.max(...xVals)) / 2;
        
        // Mirror points around center
        const data = shapeData[shape.shapeName];
        data.originalMmPoints = points.map(p => [
            2 * centerX - p[0],
            p[1]
        ]);
        
        // Reset initial position since we're updating originalMmPoints
        data.initialLeft = null;
        
        redrawShapeFromData(shape.shapeName);
        emitShapeUpdate(shape.shapeName);
    });
    
    return true;
}

// Mirror shape(s) vertically (Y axis) - supports multi-select
function mirrorY() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        
        // Get current mm points based on canvas position
        const points = getCurrentMmPoints(shape);
        if (!points) return;
        
        // Find center Y of shape
        const yVals = points.map(p => p[1]);
        const centerY = (Math.min(...yVals) + Math.max(...yVals)) / 2;
        
        // Mirror points around center
        const data = shapeData[shape.shapeName];
        data.originalMmPoints = points.map(p => [
            p[0],
            2 * centerY - p[1]
        ]);
        
        // Reset initial position since we're updating originalMmPoints
        data.initialLeft = null;
        
        redrawShapeFromData(shape.shapeName);
        emitShapeUpdate(shape.shapeName);
    });
    
    return true;
}

// Align selected shapes so their centerpoints share the same X coordinate (vertical axis)
function alignCentersVertical() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length < 2) return false;

    saveUndoState();

    // Use data.originalMmPoints directly — avoids the broken shape.left delta
    // calculation that occurs when shapes are inside an activeSelection group
    const shapeEntries = selectedShapes.map(shape => ({
        name: shape.shapeName,
        data: shapeData[shape.shapeName]
    })).filter(s => s.data && s.data.originalMmPoints);

    if (shapeEntries.length < 2) return false;

    const centerXs = shapeEntries.map(s => {
        const xVals = s.data.originalMmPoints.map(p => p[0]);
        return (Math.min(...xVals) + Math.max(...xVals)) / 2;
    });

    const targetX = centerXs.reduce((a, b) => a + b, 0) / centerXs.length;

    shapeEntries.forEach((s, i) => {
        const dx = targetX - centerXs[i];
        s.data.originalMmPoints = s.data.originalMmPoints.map(p => [p[0] + dx, p[1]]);
        s.data.initialLeft = null;
        redrawShapeFromData(s.name);
        emitShapeUpdate(s.name);
    });

    return true;
}

// Align selected shapes so their centerpoints share the same Y coordinate (horizontal axis)
function alignCentersHorizontal() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length < 2) return false;

    saveUndoState();

    // Use data.originalMmPoints directly — avoids the broken shape.left delta
    // calculation that occurs when shapes are inside an activeSelection group
    const shapeEntries = selectedShapes.map(shape => ({
        name: shape.shapeName,
        data: shapeData[shape.shapeName]
    })).filter(s => s.data && s.data.originalMmPoints);

    if (shapeEntries.length < 2) return false;

    const centerYs = shapeEntries.map(s => {
        const yVals = s.data.originalMmPoints.map(p => p[1]);
        return (Math.min(...yVals) + Math.max(...yVals)) / 2;
    });

    const targetY = centerYs.reduce((a, b) => a + b, 0) / centerYs.length;

    shapeEntries.forEach((s, i) => {
        const dy = targetY - centerYs[i];
        s.data.originalMmPoints = s.data.originalMmPoints.map(p => [p[0], p[1] + dy]);
        s.data.initialLeft = null;
        redrawShapeFromData(s.name);
        emitShapeUpdate(s.name);
    });

    return true;
}

// Distribute selected shapes with equal spacing along X (horizontal distribute)
function distributeHorizontally() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length < 3) return false;

    saveUndoState();

    const shapeEntries = selectedShapes.map(shape => ({
        name: shape.shapeName,
        data: shapeData[shape.shapeName]
    })).filter(s => s.data && s.data.originalMmPoints);

    if (shapeEntries.length < 3) return false;

    // Compute each shape's left/right/width from originalMmPoints
    shapeEntries.forEach(s => {
        const xVals = s.data.originalMmPoints.map(p => p[0]);
        s.minX = Math.min(...xVals);
        s.maxX = Math.max(...xVals);
        s.width = s.maxX - s.minX;
    });

    // Sort by left edge
    shapeEntries.sort((a, b) => a.minX - b.minX);

    const totalSpan = shapeEntries[shapeEntries.length - 1].maxX - shapeEntries[0].minX;
    const totalShapeWidth = shapeEntries.reduce((sum, s) => sum + s.width, 0);
    const gap = (totalSpan - totalShapeWidth) / (shapeEntries.length - 1);

    // Place each shape so gaps between edges are equal; keep leftmost fixed
    let cursor = shapeEntries[0].minX;
    shapeEntries.forEach((s, i) => {
        const targetMinX = i === 0 ? s.minX : cursor;
        const dx = targetMinX - s.minX;
        if (Math.abs(dx) > 0.001) {
            s.data.originalMmPoints = s.data.originalMmPoints.map(p => [p[0] + dx, p[1]]);
            s.data.initialLeft = null;
        }
        cursor = targetMinX + s.width + gap;
        redrawShapeFromData(s.name);
        emitShapeUpdate(s.name);
    });

    return true;
}

// Distribute selected shapes with equal spacing along Y (vertical distribute)
function distributeVertically() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length < 3) return false;

    saveUndoState();

    const shapeEntries = selectedShapes.map(shape => ({
        name: shape.shapeName,
        data: shapeData[shape.shapeName]
    })).filter(s => s.data && s.data.originalMmPoints);

    if (shapeEntries.length < 3) return false;

    // Compute each shape's top/bottom/height from originalMmPoints
    shapeEntries.forEach(s => {
        const yVals = s.data.originalMmPoints.map(p => p[1]);
        s.minY = Math.min(...yVals);
        s.maxY = Math.max(...yVals);
        s.height = s.maxY - s.minY;
    });

    // Sort by bottom edge
    shapeEntries.sort((a, b) => a.minY - b.minY);

    const totalSpan = shapeEntries[shapeEntries.length - 1].maxY - shapeEntries[0].minY;
    const totalShapeHeight = shapeEntries.reduce((sum, s) => sum + s.height, 0);
    const gap = (totalSpan - totalShapeHeight) / (shapeEntries.length - 1);

    // Place each shape so gaps between edges are equal; keep bottommost fixed
    let cursor = shapeEntries[0].minY;
    shapeEntries.forEach((s, i) => {
        const targetMinY = i === 0 ? s.minY : cursor;
        const dy = targetMinY - s.minY;
        if (Math.abs(dy) > 0.001) {
            s.data.originalMmPoints = s.data.originalMmPoints.map(p => [p[0], p[1] + dy]);
            s.data.initialLeft = null;
        }
        cursor = targetMinY + s.height + gap;
        redrawShapeFromData(s.name);
        emitShapeUpdate(s.name);
    });

    return true;
}

// Rotate shape 90 degrees clockwise
function rotate90() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        
        const data = shapeData[shape.shapeName];
        const points = data.originalMmPoints;
        if (!points) return;
        
        // Find center of shape
        const xVals = points.map(p => p[0]);
        const yVals = points.map(p => p[1]);
        const centerX = (Math.min(...xVals) + Math.max(...xVals)) / 2;
        const centerY = (Math.min(...yVals) + Math.max(...yVals)) / 2;
        
        // Rotate points 90 degrees clockwise around center
        data.originalMmPoints = points.map(p => [
            centerX + (p[1] - centerY),
            centerY - (p[0] - centerX)
        ]);
        
        redrawShapeFromData(shape.shapeName);
        emitShapeUpdate(shape.shapeName);
    });
    
    return true;
}

// Scale shape(s) by factor - supports multi-select
function scaleShape(factor) {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        
        const data = shapeData[shape.shapeName];
        const points = data.originalMmPoints;
        if (!points) return;
        
        // Find center of shape
        const xVals = points.map(p => p[0]);
        const yVals = points.map(p => p[1]);
        const centerX = (Math.min(...xVals) + Math.max(...xVals)) / 2;
        const centerY = (Math.min(...yVals) + Math.max(...yVals)) / 2;
        
        // Scale points around center
        data.originalMmPoints = points.map(p => [
            centerX + (p[0] - centerX) * factor,
            centerY + (p[1] - centerY) * factor
        ]);
        
        redrawShapeFromData(shape.shapeName);
        emitShapeUpdate(shape.shapeName);
    });
    
    return true;
}

// Move shape(s) to origin (0, 0) - supports multi-select
function moveToOrigin() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    // For multi-select, find the global min and move all shapes together
    let globalMinX = Infinity;
    let globalMinY = Infinity;
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        const points = shapeData[shape.shapeName].originalMmPoints;
        if (!points) return;
        
        const minX = Math.min(...points.map(p => p[0]));
        const minY = Math.min(...points.map(p => p[1]));
        globalMinX = Math.min(globalMinX, minX);
        globalMinY = Math.min(globalMinY, minY);
    });
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        
        const data = shapeData[shape.shapeName];
        const points = data.originalMmPoints;
        if (!points) return;
        
        // Shift points by global offset
        data.originalMmPoints = points.map(p => [
            p[0] - globalMinX,
            p[1] - globalMinY
        ]);
        
        redrawShapeFromData(shape.shapeName);
        emitShapeUpdate(shape.shapeName);
    });
    
    return true;
}

// Center shape(s) on work area - supports multi-select
function centerOnBed() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    // For multi-select, find the global bounds and center all shapes together
    let globalMinX = Infinity, globalMaxX = -Infinity;
    let globalMinY = Infinity, globalMaxY = -Infinity;
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        const points = shapeData[shape.shapeName].originalMmPoints;
        if (!points) return;
        
        const xVals = points.map(p => p[0]);
        const yVals = points.map(p => p[1]);
        globalMinX = Math.min(globalMinX, Math.min(...xVals));
        globalMaxX = Math.max(globalMaxX, Math.max(...xVals));
        globalMinY = Math.min(globalMinY, Math.min(...yVals));
        globalMaxY = Math.max(globalMaxY, Math.max(...yVals));
    });
    
    const width = globalMaxX - globalMinX;
    const height = globalMaxY - globalMinY;
    const targetX = (WORK_WIDTH - width) / 2;
    const targetY = (WORK_HEIGHT - height) / 2;
    const offsetX = targetX - globalMinX;
    const offsetY = targetY - globalMinY;
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        
        const data = shapeData[shape.shapeName];
        const points = data.originalMmPoints;
        if (!points) return;
        
        // Shift points
        data.originalMmPoints = points.map(p => [
            p[0] + offsetX,
            p[1] + offsetY
        ]);
        
        redrawShapeFromData(shape.shapeName);
        emitShapeUpdate(shape.shapeName);
    });
    
    return true;
}

// Duplicate selected shape
function duplicateShape() {
    const shape = getSelectedShape();
    if (!shape || !shapeData[shape.shapeName]) return null;
    
    const data = shapeData[shape.shapeName];
    const points = data.originalMmPoints;
    if (!points) return null;
    
    const newName = shape.shapeName + '_copy_' + Date.now();
    
    // Deep copy the points with small offset (20mm)
    const newPoints = points.map(p => [p[0] + 20, p[1] + 20]);
    
    addShape(newName, newPoints);
    return newName;
}

// Delete selected shape(s) - supports multi-select
function deleteShape() {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    // Hide any lingering shape tooltip
    const tooltipEl = document.getElementById('shape-tooltip');
    if (tooltipEl) tooltipEl.style.display = 'none';
    
    // Discard selection first (important for multi-select)
    canvas.discardActiveObject();
    
    const deletedNames = [];
    selectedShapes.forEach(shape => {
        const name = shape.shapeName;
        canvas.remove(shape);
        delete shapes[name];
        delete shapeData[name];
        // Clean up notch data for this shape
        delete shapeNotches[name];
        clearNotchMarksForShape(name);
        delete notchMarkObjects[name];
        deletedNames.push(name);
    });
    
    // Exit notch mode if no shapes remain
    if (Object.keys(shapes).length === 0) {
        disableNotchMode();
    } else if (notchMode) {
        showNotchNodes();
    }

    canvas.renderAll();
    
    // Notify Python
    if (window.emitEvent) {
        deletedNames.forEach(name => {
            window.emitEvent('shape_deleted', { shapeName: name });
        });
    }
    console.log('Deleted shapes:', deletedNames);
    return true;
}

// Select all shapes
function selectAll() {
    const objs = Object.values(shapes).filter(s => s && s.shapeName);
    console.log('selectAll: found', objs.length, 'shapes:', Object.keys(shapes));
    if (objs.length === 0) return;
    
    canvas.discardActiveObject();
    const sel = new fabric.ActiveSelection(objs, { canvas: canvas });
    canvas.setActiveObject(sel);
    canvas.renderAll();
}

// Linear array - repeat shape in X or Y direction
function linearArray(axis, count, spacing) {
    const shape = getSelectedShape();
    if (!shape || !shapeData[shape.shapeName]) return [];
    
    const data = shapeData[shape.shapeName];
    const points = data.originalMmPoints;
    if (!points) return [];
    
    const strokeColor = shape.stroke;
    const newNames = [];
    
    for (let i = 1; i < count; i++) {
        const newName = shape.shapeName + '_arr_' + i + '_' + Date.now();
        const offsetX = axis === 'x' ? spacing * i : 0;
        const offsetY = axis === 'y' ? spacing * i : 0;
        
        const newPoints = points.map(p => [
            p[0] + offsetX,
            p[1] + offsetY
        ]);
        
        addShape(newName, newPoints, strokeColor);
        newNames.push(newName);
    }
    
    return newNames;
}

// Grid array - create X by Y grid of copies with auto spacing
function gridArray(countX, countY) {
    const shape = getSelectedShape();
    if (!shape || !shapeData[shape.shapeName]) return [];
    
    // Get current mm points based on shape's canvas position (not stored data)
    const points = getCurrentMmPoints(shape);
    if (!points) return [];
    
    // Save state for undo
    saveUndoState();
    
    // Calculate shape bounds for auto-spacing
    const xVals = points.map(p => p[0]);
    const yVals = points.map(p => p[1]);
    const shapeWidth = Math.max(...xVals) - Math.min(...xVals);
    const shapeHeight = Math.max(...yVals) - Math.min(...yVals);
    
    // Auto spacing = shape size + 15mm buffer
    const spacingX = shapeWidth + 15;
    const spacingY = shapeHeight + 15;
    
    const strokeColor = shape.stroke;
    const newNames = [];
    
    for (let i = 0; i < countX; i++) {
        for (let j = 0; j < countY; j++) {
            if (i === 0 && j === 0) continue; // Skip original position
            
            const newName = shape.shapeName + '_grid_' + i + '_' + j + '_' + Date.now();
            const offsetX = spacingX * i;
            const offsetY = spacingY * j;
            
            const newPoints = points.map(p => [
                p[0] + offsetX,
                p[1] + offsetY
            ]);
            
            addShape(newName, newPoints, strokeColor);
            newNames.push(newName);
        }
    }
    
    console.log('Grid created:', countX, 'x', countY, 'spacing:', spacingX.toFixed(1), 'x', spacingY.toFixed(1));
    return newNames;
}

// Nesting algorithm - pack all shapes tightly to minimize bounding area
// keepOrientation: if false, will try rotating shapes for better fit
// Show a notification using NiceGUI/Quasar's notification system
function showToast(message, type = 'info', duration = 3000) {
    const colors = {
        'info': 'info',
        'success': 'positive', 
        'warning': 'warning',
        'error': 'negative'
    };
    
    // Use Quasar's notification system (same as NiceGUI's ui.notify)
    if (typeof Quasar !== 'undefined' && Quasar.Notify) {
        Quasar.Notify.create({
            message: message,
            type: colors[type] || 'info',
            position: 'bottom',
            timeout: duration === 0 ? 0 : duration,
            actions: duration === 0 ? [{ icon: 'close', color: 'white' }] : []
        });
    } else {
        console.log(`[${type}] ${message}`);
    }
}

function nestShapes(keepOrientation = true, spacing = 15) {
    console.log('=== NESTING START ===');
    
    const allShapeNames = Object.keys(shapes).filter(name => shapes[name] && shapes[name].shapeName);
    if (allShapeNames.length === 0) {
        showToast('No shapes to nest', 'warning');
        return { success: false, error: 'No shapes to nest' };
    }
    
    saveUndoState();
    
    // Get shape info for each shape
    const shapeInfos = allShapeNames.map(name => {
        const shape = shapes[name];
        const points = getCurrentMmPoints(shape);
        if (!points || points.length < 2) return null;
        
        // Simplify polygon for collision (max 60 points for better accuracy with concave shapes)
        const simplified = simplifyPolygon(points, 60);
        
        // Use full points bbox so normalizedFull always starts at 0,0 and has correct dimensions.
        // Using the simplified bbox could drop extreme vertices, giving normalizedFull negative
        // coordinates and underestimating width/height — causing collision checks to miss overlaps.
        const bboxFull = getPolygonBounds(points);
        const normalizedSimple = simplified.map(p => [p[0] - bboxFull.minX, p[1] - bboxFull.minY]);
        const normalizedFull = points.map(p => [p[0] - bboxFull.minX, p[1] - bboxFull.minY]);
        
        return {
            name: name,
            simplePoints: normalizedSimple,
            fullPoints: normalizedFull,
            width: bboxFull.maxX - bboxFull.minX,
            height: bboxFull.maxY - bboxFull.minY,
            area: (bboxFull.maxX - bboxFull.minX) * (bboxFull.maxY - bboxFull.minY),
            color: shape.stroke || '#42A5F5'
        };
    }).filter(s => s !== null);
    
    if (shapeInfos.length === 0) {
        showToast('No valid shapes', 'warning');
        return { success: false, error: 'No valid shapes' };
    }
    
    // Show starting notification
    showToast(`Nesting ${shapeInfos.length} shapes...`, 'info', 5000);  // Auto-dismiss after 5s
    
    // Try Packaide first (async call)
    nestShapesPackaide(shapeInfos, keepOrientation, spacing);
    
    return { success: true, pending: true, message: 'Nesting with Packaide...' };
}

// Async nesting using Packaide backend
async function nestShapesPackaide(shapeInfos, keepOrientation, spacing) {
    try {
        // Prepare shapes for Packaide
        const shapesForNest = shapeInfos.map(info => ({
            name: info.name,
            points: info.fullPoints,
            closed: true
        }));
        
        const response = await fetch('/nest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                shapes: shapesForNest,
                sheetWidth: rulerRightMm,
                sheetHeight: rulerTopMm,
                offset: spacing,
                rotations: keepOrientation ? 1 : 36  // 36 = every 10°, full rotation variability
            })
        });
        
        const result = await response.json();
        console.log('Packaide result:', result);
        
        if (result.status === 'ok' && result.placements && result.placements.length > 0) {
            // Apply Packaide placements
            let maxX = 0, maxY = 0;
            
            for (const placement of result.placements) {
                const data = shapeData[placement.name];
                if (!data) {
                    console.warn('Missing shapeData for', placement.name);
                    continue;
                }
                
                // Update shape with new points from Packaide
                data.originalMmPoints = placement.points;
                redrawShapeFromData(placement.name);
                emitShapeUpdate(placement.name);
                
                // Track bounds
                for (const pt of placement.points) {
                    if (pt[0] > maxX) maxX = pt[0];
                    if (pt[1] > maxY) maxY = pt[1];
                }
            }
            
            console.log(`=== PACKAIDE COMPLETE === ${result.placed} placed, ${result.failed} failed, bounds: ${maxX.toFixed(0)}x${maxY.toFixed(0)}`);
            showToast(`Nested ${result.placed} shapes to ${maxX.toFixed(0)}×${maxY.toFixed(0)}mm`, 'success', 4000);
            return;
        }
        
        // Fall back to local algorithm if Packaide fails
        console.log('Packaide returned no placements, falling back to local algorithm');
        showToast('Using local algorithm...', 'info', 2000);
        nestShapesLocal(shapeInfos, keepOrientation, spacing);
        
    } catch (error) {
        console.error('Packaide error:', error);
        console.log('Falling back to local nesting algorithm');
        showToast('Server busy, using local algorithm...', 'warning', 2000);
        nestShapesLocal(shapeInfos, keepOrientation, spacing);
    }
}

// Local nesting algorithm (fallback)
function nestShapesLocal(shapeInfos, keepOrientation, spacing) {
    // Try multiple sorting strategies and pick the best result
    const strategies = [
        { name: 'area-desc', sort: (a, b) => b.area - a.area },
        { name: 'height-desc', sort: (a, b) => b.height - a.height },
        { name: 'width-desc', sort: (a, b) => b.width - a.width },
        { name: 'perimeter-desc', sort: (a, b) => (b.width + b.height) - (a.width + a.height) },
        { name: 'maxdim-desc', sort: (a, b) => Math.max(b.width, b.height) - Math.max(a.width, a.height) },
        { name: 'area-asc', sort: (a, b) => a.area - b.area }
    ];
    
    let bestResult = null;
    let bestArea = Infinity;
    
    for (const strategy of strategies) {
        const sortedInfos = [...shapeInfos].sort(strategy.sort);
        const result = tryNestWithOrder(sortedInfos, keepOrientation, spacing);
        
        if (result.success) {
            const area = result.width * result.height;
            console.log(`Strategy ${strategy.name}: ${result.width.toFixed(0)}x${result.height.toFixed(0)} = ${area.toFixed(0)}`);
            if (area < bestArea) {
                bestArea = area;
                bestResult = result;
            }
        }
    }
    
    if (!bestResult) {
        undo();
        console.log('=== NESTING FAILED ===');
        showToast('Could not nest shapes - not enough space', 'error', 4000);
        return;
    }
    
    // Apply the best result
    for (const p of bestResult.placements) {
        const data = shapeData[p.info.name];
        if (!data) {
            console.error('Missing shapeData for', p.info.name);
            continue;
        }
        data.originalMmPoints = p.full;
        redrawShapeFromData(p.info.name);
        emitShapeUpdate(p.info.name);
    }
    
    console.log('=== LOCAL NESTING COMPLETE ===', bestResult.width.toFixed(0), 'x', bestResult.height.toFixed(0));
    showToast(`Nested to ${bestResult.width.toFixed(0)}×${bestResult.height.toFixed(0)}mm`, 'success', 4000);
}

// Try nesting with a specific shape order
function tryNestWithOrder(shapeInfos, keepOrientation, spacing) {
    const placedShapes = [];
    
    for (let i = 0; i < shapeInfos.length; i++) {
        const info = shapeInfos[i];
        
        let bestPos = null, bestRotation = 0;
        let bestSimple = null, bestFull = null;
        let bestScore = Infinity;
        
        const rotations = keepOrientation ? [0] : [0, 90, 180, 270];
        
        for (const rotation of rotations) {
            const simple = rotateAndNormalize(info.simplePoints, rotation, info.width/2, info.height/2);
            const full = rotateAndNormalize(info.fullPoints, rotation, info.width/2, info.height/2);
            // Use full polygon bounds for w/h — simple can drop extreme vertices and underestimate size
            const bboxFull = getPolygonBounds(full);
            const w = bboxFull.maxX - bboxFull.minX, h = bboxFull.maxY - bboxFull.minY;
            
            // Generate candidates from placed shape vertices
            const candidates = generateCandidates(placedShapes, w, h, spacing);
            
            for (const pos of candidates) {
                if (pos.x + w > rulerRightMm || pos.y + h > rulerTopMm) continue;
                
                // Use full polygon for collision detection — simplified polygon can miss extreme
                // vertices (stride-based simplification skips them), causing undetected overlaps.
                const testFull = full.map(p => [p[0] + pos.x, p[1] + pos.y]);
                const testSimple = simple.map(p => [p[0] + pos.x, p[1] + pos.y]);
                const testBBox = { minX: pos.x, maxX: pos.x + w, minY: pos.y, maxY: pos.y + h };
                
                // Early collision check before computing score
                let collides = false;
                for (const placed of placedShapes) {
                    if (!bboxOverlap(testBBox, placed.bbox, spacing)) continue;
                    if (polygonsCollide(testFull, placed.full, spacing)) {
                        collides = true;
                        break;
                    }
                }
                if (collides) continue;
                
                // Score: prioritize minimizing bounding area, with tie-breaker for bottom-left
                let maxX = pos.x + w, maxY = pos.y + h;
                for (const placed of placedShapes) {
                    if (placed.bbox.maxX > maxX) maxX = placed.bbox.maxX;
                    if (placed.bbox.maxY > maxY) maxY = placed.bbox.maxY;
                }
                // Primary: bounding area, Secondary: prefer bottom-left (lower y, then lower x)
                const score = maxX * maxY + (pos.y * 0.001 + pos.x * 0.0001);
                
                if (score < bestScore) {
                    bestScore = score;
                    bestPos = pos;
                    bestRotation = rotation;
                    bestSimple = testSimple;  // kept for vertex-based candidate generation
                    bestFull = testFull;
                }
            }
        }
        
        if (!bestPos) {
            return { success: false, error: `Could not place: ${info.name}` };
        }
        
        placedShapes.push({
            simple: bestSimple,
            full: bestFull,
            bbox: getPolygonBounds(bestFull),  // Use full polygon bbox — simple can miss extreme vertices
            info: info
        });
    }
    
    // Calculate final bounds
    let maxX = 0, maxY = 0;
    for (const p of placedShapes) {
        if (p.bbox.maxX > maxX) maxX = p.bbox.maxX;
        if (p.bbox.maxY > maxY) maxY = p.bbox.maxY;
    }
    
    if (maxX > rulerRightMm || maxY > rulerTopMm) {
        return { success: false, error: `Need ${maxX.toFixed(0)}×${maxY.toFixed(0)}mm` };
    }
    
    return { success: true, width: maxX, height: maxY, placements: placedShapes };
}

function simplifyPolygon(points, maxPoints) {
    if (points.length <= maxPoints) return points.map(p => [p[0], p[1]]);
    const step = Math.ceil(points.length / maxPoints);
    const result = [];
    for (let i = 0; i < points.length; i += step) result.push([points[i][0], points[i][1]]);
    return result;
}

function rotateAndNormalize(points, degrees, cx, cy) {
    if (degrees === 0) return points.map(p => [p[0], p[1]]);
    const rad = degrees * Math.PI / 180;
    const cos = Math.cos(rad), sin = Math.sin(rad);
    const rotated = points.map(p => {
        const x = p[0] - cx, y = p[1] - cy;
        return [x * cos - y * sin + cx, x * sin + y * cos + cy];
    });
    const bbox = getPolygonBounds(rotated);
    return rotated.map(p => [p[0] - bbox.minX, p[1] - bbox.minY]);
}

function generateCandidates(placedShapes, width, height, spacing) {
    const candidates = [{x: 0, y: 0}];
    
    // Add grid positions for first shape or when few candidates
    if (placedShapes.length === 0) {
        for (let y = 0; y <= rulerTopMm - height; y += 50) {
            for (let x = 0; x <= rulerRightMm - width; x += 50) {
                candidates.push({x, y});
            }
        }
    }
    
    for (const placed of placedShapes) {
        const b = placed.bbox;
        
        // Standard positions: right of shape, above shape
        candidates.push({x: b.maxX + spacing, y: 0});
        candidates.push({x: b.maxX + spacing, y: b.minY});
        candidates.push({x: b.maxX + spacing, y: b.maxY - height});
        candidates.push({x: 0, y: b.maxY + spacing});
        candidates.push({x: b.minX, y: b.maxY + spacing});
        candidates.push({x: b.maxX - width, y: b.maxY + spacing});
        
        // Try positions along right edge of placed shape
        for (let y = b.minY; y <= b.maxY; y += 25) {
            candidates.push({x: b.maxX + spacing, y: Math.max(0, y - height)});
            candidates.push({x: b.maxX + spacing, y: y});
        }
        
        // Try positions along top edge of placed shape
        for (let x = b.minX; x <= b.maxX; x += 25) {
            candidates.push({x: x, y: b.maxY + spacing});
            candidates.push({x: Math.max(0, x - width), y: b.maxY + spacing});
        }
        
        // Add vertex positions for concave fitting
        for (let i = 0; i < placed.simple.length; i++) {
            const pt = placed.simple[i];
            candidates.push({x: pt[0] + spacing, y: 0});
            candidates.push({x: pt[0] + spacing, y: Math.max(0, pt[1] - height)});
            candidates.push({x: 0, y: pt[1] + spacing});
            candidates.push({x: Math.max(0, pt[0] - width), y: pt[1] + spacing});
            
            // Position shape corner at vertex + spacing
            candidates.push({x: pt[0] + spacing, y: pt[1] + spacing});
            candidates.push({x: pt[0] + spacing, y: Math.max(0, pt[1] - height - spacing)});
            candidates.push({x: Math.max(0, pt[0] - width - spacing), y: pt[1] + spacing});
        }
    }
    
    // Dedupe with finer resolution
    const seen = new Set();
    return candidates.filter(c => {
        if (c.x < 0 || c.y < 0) return false;
        const key = Math.round(c.x / 5) + ',' + Math.round(c.y / 5);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    }).sort((a, b) => (a.y * 10000 + a.x) - (b.y * 10000 + b.x));
}

function getPolygonBounds(points) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of points) {
        if (p[0] < minX) minX = p[0];
        if (p[0] > maxX) maxX = p[0];
        if (p[1] < minY) minY = p[1];
        if (p[1] > maxY) maxY = p[1];
    }
    return { minX, maxX, minY, maxY };
}

function bboxOverlap(a, b, spacing) {
    return !(a.maxX + spacing <= b.minX || b.maxX + spacing <= a.minX ||
             a.maxY + spacing <= b.minY || b.maxY + spacing <= a.minY);
}

function polygonsCollide(poly1, poly2, spacing) {
    // Edge intersection check
    for (let i = 0; i < poly1.length; i++) {
        const a1 = poly1[i], a2 = poly1[(i + 1) % poly1.length];
        for (let j = 0; j < poly2.length; j++) {
            const b1 = poly2[j], b2 = poly2[(j + 1) % poly2.length];
            if (segmentsIntersect(a1, a2, b1, b2)) return true;
        }
    }
    
    // Containment check - check ALL points, not just some
    for (const pt of poly1) {
        if (pointInPolygon(pt, poly2)) return true;
    }
    for (const pt of poly2) {
        if (pointInPolygon(pt, poly1)) return true;
    }
    
    // Spacing check - vertex to edge distance
    const spacingSq = spacing * spacing;
    for (const p of poly1) {
        for (let j = 0; j < poly2.length; j++) {
            const dist = pointToSegmentDistSq(p, poly2[j], poly2[(j + 1) % poly2.length]);
            if (dist < spacingSq) return true;
        }
    }
    for (const p of poly2) {
        for (let j = 0; j < poly1.length; j++) {
            const dist = pointToSegmentDistSq(p, poly1[j], poly1[(j + 1) % poly1.length]);
            if (dist < spacingSq) return true;
        }
    }
    
    return false;
}

// Squared distance from point to line segment
function pointToSegmentDistSq(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const lenSq = dx * dx + dy * dy;
    if (lenSq === 0) return (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2;
    
    let t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));
    
    const cx = a[0] + t * dx, cy = a[1] + t * dy;
    return (p[0] - cx) ** 2 + (p[1] - cy) ** 2;
}

function segmentsIntersect(a1, a2, b1, b2) {
    const d1 = (b2[0]-b1[0])*(a1[1]-b1[1]) - (b2[1]-b1[1])*(a1[0]-b1[0]);
    const d2 = (b2[0]-b1[0])*(a2[1]-b1[1]) - (b2[1]-b1[1])*(a2[0]-b1[0]);
    const d3 = (a2[0]-a1[0])*(b1[1]-a1[1]) - (a2[1]-a1[1])*(b1[0]-a1[0]);
    const d4 = (a2[0]-a1[0])*(b2[1]-a1[1]) - (a2[1]-a1[1])*(b2[0]-a1[0]);
    return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

function pointInPolygon(pt, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        if (((poly[i][1] > pt[1]) !== (poly[j][1] > pt[1])) &&
            (pt[0] < (poly[j][0] - poly[i][0]) * (pt[1] - poly[i][1]) / (poly[j][1] - poly[i][1]) + poly[i][0])) {
            inside = !inside;
        }
    }
    return inside;
}

// Rotate points around a center point
function rotatePointsAroundCenter(points, degrees, cx, cy) {
    const rad = degrees * Math.PI / 180;
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);
    
    return points.map(p => {
        const x = p[0] - cx;
        const y = p[1] - cy;
        return [
            x * cos - y * sin + cx,
            x * sin + y * cos + cy
        ];
    });
}

// Mirror copy - duplicate and flip
function mirrorCopy(axis) {
    const shape = getSelectedShape();
    if (!shape || !shapeData[shape.shapeName]) return null;
    
    // Get current mm points based on shape's canvas position
    const points = getCurrentMmPoints(shape);
    if (!points) return null;
    
    // Find bounds
    const xVals = points.map(p => p[0]);
    const yVals = points.map(p => p[1]);
    const minX = Math.min(...xVals);
    const maxX = Math.max(...xVals);
    const minY = Math.min(...yVals);
    const maxY = Math.max(...yVals);
    
    const strokeColor = shape.stroke;
    const newName = shape.shapeName + '_mirror_' + Date.now();
    let newPoints;
    
    if (axis === 'x') {
        // Mirror to the right
        newPoints = points.map(p => [
            2 * maxX - p[0],
            p[1]
        ]);
    } else {
        // Mirror above
        newPoints = points.map(p => [
            p[0],
            2 * maxY - p[1]
        ]);
    }
    
    addShape(newName, newPoints, strokeColor);
    return newName;
}

// Helper: Redraw a single shape from its data
function redrawShapeFromData(shapeName) {
    console.log('redrawShapeFromData:', shapeName, 'exists in shapes:', !!shapes[shapeName], 'exists in shapeData:', !!shapeData[shapeName]);
    
    if (!shapeData[shapeName]) {
        console.error('No shapeData for', shapeName);
        return;
    }
    
    const data = shapeData[shapeName];
    const oldShape = shapes[shapeName];
    const points = data.originalMmPoints;
    if (!points) {
        console.error('No points for', shapeName);
        return;
    }
    
    const strokeColor = oldShape ? oldShape.stroke : '#42A5F5';
    if (oldShape) {
        canvas.remove(oldShape);
    }
    
    const canvasPoints = points.map(p => ({
        x: toCanvasX(p[0]),
        y: toCanvasY(p[1])
    }));
    
    const newShape = new fabric.Polyline(canvasPoints, {
        fill: 'transparent',
        stroke: strokeColor,
        strokeWidth: 1,
        selectable: true,
        hasControls: true,
        hasBorders: true,
        lockRotation: false,
        lockScalingX: false,
        lockScalingY: false,
        cornerColor: strokeColor,
        borderColor: strokeColor,
        cornerSize: 10,
        cornerStyle: 'circle',
        transparentCorners: false,
        objectCaching: false,
        shapeName: shapeName
    });
    
    shapes[shapeName] = newShape;
    canvas.add(newShape);
    canvas.setActiveObject(newShape);
    // Compensate for Fabric.js v5 Polyline strokeWidth/2 bounding box shift
    newShape.set({ left: newShape.left + newShape.strokeWidth / 2, top: newShape.top + newShape.strokeWidth / 2 });
    
    // Update initial position
    data.initialLeft = newShape.left;
    data.initialTop = newShape.top;
    
    // Redraw notch marks for this shape (they move with the shape)
    drawNotchMarksForShape(shapeName);
    // If in notch mode, refresh the node circles
    if (notchMode) showNotchNodes();
    
    // Keep ruler handles on top of everything
    ensureRulerHandlesFront();
    
    console.log('redrawShapeFromData done:', shapeName, 'now in shapes:', !!shapes[shapeName], 'total shapes:', Object.keys(shapes).length);
    canvas.renderAll();
}

// Helper: Emit shape update to Python
function emitShapeUpdate(shapeName) {
    if (!shapeData[shapeName]) return;
    
    const data = shapeData[shapeName];
    const points = data.originalMmPoints;
    if (!points) return;
    
    if (window.emitEvent) {
        window.emitEvent('shape_moved', {
            shapeName: shapeName,
            newPoints: points
        });
    }
}

// Clipboard for copy/paste
let clipboard = null;

// Save current state to undo stack
function saveUndoState() {
    const state = { shapes: {}, notches: {} };
    Object.keys(shapeData).forEach(name => {
        if (shapeData[name] && shapeData[name].originalMmPoints) {
            state.shapes[name] = {
                points: shapeData[name].originalMmPoints.map(p => [p[0], p[1]]),
                stroke: shapes[name] ? shapes[name].stroke : '#42A5F5'
            };
        }
    });
    // Save notch edge keys
    Object.keys(shapeNotches).forEach(name => {
        if (shapeNotches[name] && shapeNotches[name].size > 0) {
            state.notches[name] = [...shapeNotches[name].values()];
        }
    });
    undoStack.push(JSON.stringify(state));
    if (undoStack.length > MAX_UNDO) {
        undoStack.shift();
    }
}

// Undo last action
function undo() {
    if (undoStack.length === 0) {
        console.log('Nothing to undo');
        return false;
    }
    
    const saved = JSON.parse(undoStack.pop());
    const state = saved.shapes || saved;  // backwards compat if state is just shapes
    const notchState = saved.notches || {};
    
    // Clear current shapes and notch marks
    Object.values(shapes).forEach(shape => canvas.remove(shape));
    Object.values(notchMarkObjects).forEach(arr => arr.forEach(obj => canvas.remove(obj)));
    shapes = {};
    shapeData = {};
    shapeNotches = {};
    notchMarkObjects = {};
    
    // Restore shapes from state
    Object.keys(state).forEach(name => {
        addShape(name, state[name].points, state[name].stroke);
    });
    
    // Restore notch edge keys and redraw marks
    Object.keys(notchState).forEach(name => {
        if (notchState[name] && notchState[name].length > 0) {
            shapeNotches[name] = new Map(notchState[name].map(k => [k.edgeIdx, k]));
            drawNotchMarksForShape(name);
        }
    });
    
    if (notchMode) showNotchNodes();
    canvas.discardActiveObject();
    canvas.renderAll();
    console.log('Undo applied');
    return true;
}

// Copy selected shape to clipboard
function copyShape() {
    const shape = getSelectedShape();
    if (!shape || !shapeData[shape.shapeName]) return false;
    
    const data = shapeData[shape.shapeName];
    if (!data.originalMmPoints) return false;
    
    // Deep copy the points
    clipboard = {
        points: data.originalMmPoints.map(p => [p[0], p[1]]),
        stroke: shape.stroke
    };
    console.log('Copied shape to clipboard');
    return true;
}

// Paste shape from clipboard
function pasteShape() {
    if (!clipboard || !clipboard.points) return null;
    
    const newName = 'pasted_' + Date.now();
    
    // Offset by 10mm up and to the right
    const newPoints = clipboard.points.map(p => [p[0] + 10, p[1] + 10]);
    
    // Pass the original stroke color
    addShape(newName, newPoints, clipboard.stroke);
    console.log('Pasted shape:', newName);
    return newName;
}

// Rotate shape(s) by exact degrees - supports multi-select
function rotateByDegrees(degrees) {
    const selectedShapes = getSelectedShapes();
    if (selectedShapes.length === 0) return false;
    
    saveUndoState();
    
    // Convert degrees to radians
    const radians = degrees * Math.PI / 180;
    const cos = Math.cos(radians);
    const sin = Math.sin(radians);
    
    selectedShapes.forEach(shape => {
        if (!shapeData[shape.shapeName]) return;
        
        const data = shapeData[shape.shapeName];
        const points = data.originalMmPoints;
        if (!points) return;
        
        // Find center of shape
        const xVals = points.map(p => p[0]);
        const yVals = points.map(p => p[1]);
        const centerX = (Math.min(...xVals) + Math.max(...xVals)) / 2;
        const centerY = (Math.min(...yVals) + Math.max(...yVals)) / 2;
        
        // Rotate points around center
        data.originalMmPoints = points.map(p => [
            centerX + (p[0] - centerX) * cos - (p[1] - centerY) * sin,
            centerY + (p[0] - centerX) * sin + (p[1] - centerY) * cos
        ]);
        
        redrawShapeFromData(shape.shapeName);
        emitShapeUpdate(shape.shapeName);
    });
    
    return true;
}

// Keyboard event handler
function handleKeyDown(e) {
    // Don't handle if typing in an input field
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    
    // Cmd/Ctrl + Z = Undo
    if ((e.metaKey || e.ctrlKey) && e.key === 'z') {
        if (undo()) {
            e.preventDefault();
        }
    }
    
    // Cmd/Ctrl + A = Select All
    if ((e.metaKey || e.ctrlKey) && e.key === 'a') {
        selectAll();
        e.preventDefault();
    }
    
    // Cmd/Ctrl + C = Copy
    if ((e.metaKey || e.ctrlKey) && e.key === 'c') {
        if (copyShape()) {
            e.preventDefault();
        }
    }
    
    // Cmd/Ctrl + V = Paste
    if ((e.metaKey || e.ctrlKey) && e.key === 'v') {
        if (pasteShape()) {
            e.preventDefault();
        }
    }
    
    // Delete or Backspace = Delete shape
    if (e.key === 'Delete' || e.key === 'Backspace') {
        if (deleteShape()) {
            e.preventDefault();
        }
    }

    // Arrow keys = nudge selected shape(s) by 1mm (or 10mm with Shift)
    if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
        const active = canvas.getActiveObject();
        if (!active) return;

        const step = e.shiftKey ? 10 : 1;  // mm
        let dx = 0, dy = 0;
        if (e.key === 'ArrowLeft')  dx = -step;
        if (e.key === 'ArrowRight') dx =  step;
        if (e.key === 'ArrowUp')    dy =  step;
        if (e.key === 'ArrowDown')  dy = -step;

        // Collect shape names to move
        const shapeNames = active.type === 'activeSelection'
            ? active.getObjects().map(o => o.shapeName).filter(Boolean)
            : (active.shapeName ? [active.shapeName] : []);

        if (shapeNames.length === 0) return;

        saveUndoState();

        shapeNames.forEach(name => {
            const data = shapeData[name];
            if (!data || !data.originalMmPoints) return;

            let newPoints = data.originalMmPoints.map(p => [p[0] + dx, p[1] + dy]);

            // Constrain to ruler-bounded work area
            const xs = newPoints.map(p => p[0]);
            const ys = newPoints.map(p => p[1]);
            let cx = 0, cy = 0;
            if (Math.min(...xs) < 0)                  cx = -Math.min(...xs);
            else if (Math.max(...xs) > rulerRightMm)  cx = rulerRightMm - Math.max(...xs);
            if (Math.min(...ys) < 0)                  cy = -Math.min(...ys);
            else if (Math.max(...ys) > rulerTopMm)    cy = rulerTopMm - Math.max(...ys);
            if (cx !== 0 || cy !== 0) {
                newPoints = newPoints.map(p => [p[0] + cx, p[1] + cy]);
            }

            data.originalMmPoints = newPoints;
            emitShapeUpdate(name);
        });

        // Redraw all moved shapes
        shapeNames.forEach(name => redrawShapeFromData(name));

        // Restore multi-selection if needed
        if (shapeNames.length > 1) {
            const objs = shapeNames.map(n => shapes[n]).filter(Boolean);
            canvas.discardActiveObject();
            const sel = new fabric.ActiveSelection(objs, { canvas });
            canvas.setActiveObject(sel);
        }

        canvas.renderAll();
        e.preventDefault();
    }
}

// Initialize keyboard listeners
document.addEventListener('keydown', handleKeyDown);

// Save canvas state to JSON
function saveCanvasState() {
    const state = {
        version: 2,
        timestamp: new Date().toISOString(),
        shapes: {},
        notches: {}
    };
    
    Object.keys(shapeData).forEach(name => {
        const data = shapeData[name];
        const shape = shapes[name];
        if (data && data.originalMmPoints && shape) {
            state.shapes[name] = {
                points: data.originalMmPoints,
                color: shape.stroke || '#42A5F5'
            };
        }
    });
    
    // Save notch edge keys
    Object.keys(shapeNotches).forEach(name => {
        if (shapeNotches[name] && shapeNotches[name].size > 0) {
            state.notches[name] = [...shapeNotches[name].values()];
        }
    });
    
    return JSON.stringify(state, null, 2);
}

// Load canvas state from JSON
function loadCanvasState(jsonString) {
    try {
        const state = JSON.parse(jsonString);
        
        if (!state.shapes) {
            throw new Error('Invalid canvas state: no shapes found');
        }
        
        // Clear existing shapes and notch data
        clearShapes();
        
        // Add each shape
        let colorIndex = 0;
        Object.keys(state.shapes).forEach(name => {
            const shapeState = state.shapes[name];
            if (shapeState.points && shapeState.points.length > 0) {
                addShape(name, shapeState.points, shapeState.color || colorIndex, shapeState.segmentBreaks || [0]);
                colorIndex++;
            }
        });
        
        // Restore notch edge keys and draw marks
        if (state.notches) {
            Object.keys(state.notches).forEach(name => {
                if (state.notches[name] && state.notches[name].length > 0) {
                    shapeNotches[name] = new Map(state.notches[name].map(k => [k.edgeIdx, k]));
                    drawNotchMarksForShape(name);
                }
            });
        }
        
        if (notchMode) showNotchNodes();
        console.log('Loaded canvas state with', Object.keys(state.shapes).length, 'shapes');
        return true;
    } catch (e) {
        console.error('Failed to load canvas state:', e);
        throw e;
    }
}

// Get all shape data for saving
function getCanvasData() {
    const data = {};
    Object.keys(shapeData).forEach(name => {
        const shape = shapes[name];
        if (shapeData[name] && shapeData[name].originalMmPoints && shape) {
            data[name] = {
                points: shapeData[name].originalMmPoints,
                segmentBreaks: shapeData[name].segmentBreaks || [0],
                color: shape.stroke || '#42A5F5'
            };
        }
    });
    return data;
}

// ============ NOTCH TOOL ============

// Compute signed polygon area (shoelace formula, mm coords)
// Positive = CCW in standard math coords (Y up)
function signedPolygonArea(points) {
    let area = 0;
    const n = points.length;
    for (let i = 0; i < n; i++) {
        const j = (i + 1) % n;
        area += points[i][0] * points[j][1];
        area -= points[j][0] * points[i][1];
    }
    return area / 2;
}

// Get the unit tangent and inward normal at a path node (mm coords)
function getShapeTangentAndNormal(points, nodeIdx) {
    const n = points.length;
    const prev = points[(nodeIdx - 1 + n) % n];
    const next = points[(nodeIdx + 1) % n];

    let tx = next[0] - prev[0];
    let ty = next[1] - prev[1];
    const len = Math.sqrt(tx * tx + ty * ty);
    if (len < 1e-9) { tx = 1; ty = 0; } else { tx /= len; ty /= len; }

    // For CCW polygon (area > 0), interior is to the LEFT of travel: inward = (-ty, tx)
    // For CW  polygon (area < 0), interior is to the RIGHT:           inward = ( ty,-tx)
    const area = signedPolygonArea(points);
    let nx, ny;
    if (area >= 0) {
        nx = -ty; ny = tx;
    } else {
        nx = ty; ny = -tx;
    }
    return { tangent: [tx, ty], inward: [nx, ny] };
}

// Compute notch V geometry for a node (mm coords)
// V: two base points on the boundary, apex inward — like a sewing notch
// Returns { apex, e1, e2 } — e1/e2 are the boundary-side open ends of the V
function computeNotchGeometry(points, nodeKey) {
    const p = [nodeKey.x, nodeKey.y];
    const n = points.length;
    // edgeIdx may be fractional (junction nodes use breaks[k]-0.5); round to get real index
    const ei = Math.round(nodeKey.edgeIdx);
    const safeEi = Math.min(Math.max(ei, 0), n - 1);

    // Outgoing edge tangent (edge leaving ei → ei+1)
    const bOut = points[(safeEi + 1) % n];
    const outDx = bOut[0] - points[safeEi][0], outDy = bOut[1] - points[safeEi][1];
    const outLen = Math.sqrt(outDx*outDx + outDy*outDy);
    let tx = outLen > 1e-9 ? outDx/outLen : 1;
    let ty = outLen > 1e-9 ? outDy/outLen : 0;

    // For junction nodes (fractional edgeIdx), average with the incoming tangent so the
    // V-mark is symmetric about the join and doesn't appear rotated to one side.
    if (nodeKey.edgeIdx !== Math.floor(nodeKey.edgeIdx)) {
        const prevIdx = (safeEi - 1 + n) % n;
        const inDx = points[safeEi][0] - points[prevIdx][0];
        const inDy = points[safeEi][1] - points[prevIdx][1];
        const inLen = Math.sqrt(inDx*inDx + inDy*inDy);
        if (inLen > 1e-9) {
            // Average the two unit tangents then re-normalise
            let avgX = tx + inDx/inLen;
            let avgY = ty + inDy/inLen;
            const avgLen = Math.sqrt(avgX*avgX + avgY*avgY);
            if (avgLen > 1e-9) { tx = avgX/avgLen; ty = avgY/avgLen; }
        }
    }

    const area = signedPolygonArea(points);
    let nx, ny;
    if (area >= 0) { nx = -ty; ny = tx; }
    else           { nx = ty;  ny = -tx; }

    const HALF_WIDTH = 4;  // mm — half the opening width along the boundary
    const DEPTH = 5;       // mm — how deep the apex goes into the shape

    // Base points: on the boundary, either side of the midpoint
    const e1 = [p[0] + HALF_WIDTH * tx, p[1] + HALF_WIDTH * ty];
    const e2 = [p[0] - HALF_WIDTH * tx, p[1] - HALF_WIDTH * ty];

    // Apex: inside the shape
    const apex = [p[0] + DEPTH * nx, p[1] + DEPTH * ny];

    return { apex, e1, e2, nodePoint: p };
}

// Compute notch nodes for a shape:
//   - One node at the midpoint of every segment (spline or line)
//   - One junction node at each segment boundary that is NOT a sharp corner
function computeCardinalNodes(shapeName) {
    const data = shapeData[shapeName];
    if (!data || !data.originalMmPoints) return [];
    const pts = data.originalMmPoints;
    const n = pts.length;

    const CORNER_THRESHOLD_DEG = 20;  // matches toolpath_generator.py corner_angle_threshold
    const CORNER_COS = Math.cos(CORNER_THRESHOLD_DEG * Math.PI / 180);

    const breaks = (data.segmentBreaks && data.segmentBreaks.length > 0)
        ? data.segmentBreaks : [0];
    // segmentTypes[i] is the DXF entity type ('SPLINE', 'LINE', 'ARC', etc.) for segment i
    const types = (data.segmentTypes && data.segmentTypes.length > 0)
        ? data.segmentTypes : [];

    // Determine whether a segment index represents a curved (spline-like) entity
    function isCurved(si) {
        const t = types[si] || '';
        return t === 'SPLINE' || t === 'ARC';
    }

    const nodes = [];

    // --- One midpoint node per segment (every DXF entity gets exactly one node) ---
    for (let si = 0; si < breaks.length; si++) {
        const start = breaks[si];
        const end   = (si + 1 < breaks.length) ? breaks[si + 1] : n - 1;
        if (end <= start) continue;
        let totalLen = 0;
        for (let i = start; i < end; i++) {
            const dx = pts[i+1][0]-pts[i][0], dy = pts[i+1][1]-pts[i][1];
            totalLen += Math.sqrt(dx*dx+dy*dy);
        }
        if (totalLen < 0.01) continue;
        const half = totalLen / 2;
        let walked = 0, mx = pts[start][0], my = pts[start][1];
        for (let i = start; i < end; i++) {
            const dx = pts[i+1][0]-pts[i][0], dy = pts[i+1][1]-pts[i][1];
            const sl = Math.sqrt(dx*dx+dy*dy);
            if (walked + sl >= half) {
                const t = (half - walked) / sl;
                mx = pts[i][0]+t*dx; my = pts[i][1]+t*dy; break;
            }
            walked += sl;
        }
        nodes.push({ edgeIdx: start + (end - start) / 2, x: mx, y: my });
    }

    // --- Junction node at segment boundaries ONLY between two curved entities ---
    // This prevents junction nodes at straight-line connector boundaries (notch cuts, etc.)
    for (let si = 1; si < breaks.length; si++) {
        // Both the segment ending here (si-1) and the segment starting here (si) must be curved
        if (!isCurved(si - 1) || !isCurved(si)) continue;

        const jIdx = breaks[si];
        if (jIdx <= 0 || jIdx >= n - 1) continue;

        const inDx = pts[jIdx][0]-pts[jIdx-1][0], inDy = pts[jIdx][1]-pts[jIdx-1][1];
        const inLen = Math.sqrt(inDx*inDx+inDy*inDy);
        const outDx = pts[jIdx+1][0]-pts[jIdx][0], outDy = pts[jIdx+1][1]-pts[jIdx][1];
        const outLen = Math.sqrt(outDx*outDx+outDy*outDy);
        if (inLen < 1e-9 || outLen < 1e-9) continue;

        const dot = (inDx/inLen)*(outDx/outLen) + (inDy/inLen)*(outDy/outLen);
        if (dot >= CORNER_COS) {
            nodes.push({ edgeIdx: jIdx - 0.5, x: pts[jIdx][0], y: pts[jIdx][1] });
        }
    }

    // --- Wrap-around junction for closed shapes (only if both end segments are curved) ---
    if (breaks.length > 1) {
        const lastSi = breaks.length - 1;
        if (isCurved(lastSi) && isCurved(0)) {
            const dx0 = pts[n-1][0]-pts[0][0], dy0 = pts[n-1][1]-pts[0][1];
            if (Math.sqrt(dx0*dx0+dy0*dy0) < 1.0) {
                const inDx = pts[n-1][0]-pts[n-2][0], inDy = pts[n-1][1]-pts[n-2][1];
                const inLen = Math.sqrt(inDx*inDx+inDy*inDy);
                const outDx = pts[1][0]-pts[0][0], outDy = pts[1][1]-pts[0][1];
                const outLen = Math.sqrt(outDx*outDx+outDy*outDy);
                if (inLen > 1e-9 && outLen > 1e-9) {
                    const dot = (inDx/inLen)*(outDx/outLen)+(inDy/inLen)*(outDy/outLen);
                    if (dot >= CORNER_COS) {
                        nodes.push({ edgeIdx: -0.5, x: pts[0][0], y: pts[0][1] });
                    }
                }
            }
        }
    }

    return nodes;
}

// Render clickable node circles for all shapes (shown only in notch mode)
function showNotchNodes() {
    hideNotchNodes();

    Object.keys(shapeData).forEach(shapeName => {
        const data = shapeData[shapeName];
        if (!data || !data.originalMmPoints) return;

        const nodes = computeCardinalNodes(shapeName);
        nodes.forEach((nodeKey) => {
            const cx = toCanvasX(nodeKey.x);
            const cy = toCanvasY(nodeKey.y);

            const isActive = shapeNotches[shapeName] && shapeNotches[shapeName].has(nodeKey.edgeIdx);

            const circle = new fabric.Circle({
                left: cx,
                top: cy,
                originX: 'center',
                originY: 'center',
                radius: 8,
                fill: isActive ? 'rgba(255, 107, 53, 0.85)' : 'rgba(255,255,255,0.12)',
                stroke: isActive ? '#FF6B35' : '#aaaaaa',
                strokeWidth: 2,
                selectable: false,
                evented: true,
                hoverCursor: 'pointer',
                _notchNode: true,
                _shapeName: shapeName,
                _nodeKey: nodeKey
            });

            circle.on('mousedown', function() {
                toggleNotch(shapeName, nodeKey);
            });

            notchNodeObjects.push(circle);
            canvas.add(circle);
            canvas.bringToFront(circle);
        });
    });

    canvas.renderAll();
}

// Remove all node circles from canvas
function hideNotchNodes() {
    notchNodeObjects.forEach(obj => canvas.remove(obj));
    notchNodeObjects = [];
    canvas.renderAll();
}

// Toggle notch mode on/off (called from Python toolbar button)
// Internally disable notch mode and notify Python to reset the toolbar button
function disableNotchMode() {
    notchMode = false;
    hideNotchNodes();
    Object.values(shapes).forEach(shape => { shape.selectable = true; });
    if (window.emitEvent) window.emitEvent('notch_mode_changed', { active: false });
    canvas.renderAll();
}

function setNotchMode(active) {
    notchMode = active;
    if (active) {
        Object.values(shapes).forEach(shape => { shape.selectable = false; });
        canvas.discardActiveObject();
        showNotchNodes();
    } else {
        hideNotchNodes();
        Object.values(shapes).forEach(shape => { shape.selectable = true; });
    }
    canvas.renderAll();
}

// Toggle a notch on/off at a specific edge node
function toggleNotch(shapeName, nodeKey) {
    if (!shapeNotches[shapeName]) shapeNotches[shapeName] = new Map();

    if (shapeNotches[shapeName].has(nodeKey.edgeIdx)) {
        shapeNotches[shapeName].delete(nodeKey.edgeIdx);
    } else {
        shapeNotches[shapeName].set(nodeKey.edgeIdx, nodeKey);
    }

    drawNotchMarksForShape(shapeName);
    showNotchNodes();  // Refresh highlighted state of node circles
}

// Draw the V marks for all active notches on a shape
function drawNotchMarksForShape(shapeName) {
    clearNotchMarksForShape(shapeName);

    const data = shapeData[shapeName];
    if (!data || !data.originalMmPoints) return;

    const notches = shapeNotches[shapeName];
    if (!notches || notches.size === 0) return;

    if (!notchMarkObjects[shapeName]) notchMarkObjects[shapeName] = [];

    notches.forEach((nodeKey) => {
        const { apex, e1, e2 } = computeNotchGeometry(data.originalMmPoints, nodeKey);

        const ax  = toCanvasX(apex[0]) + 0.25, ay  = toCanvasY(apex[1]) + 0.25;
        const e1x = toCanvasX(e1[0])   + 0.25, e1y = toCanvasY(e1[1])   + 0.25;
        const e2x = toCanvasX(e2[0])   + 0.25, e2y = toCanvasY(e2[1])   + 0.25;

        const line1 = new fabric.Line([e1x, e1y, ax, ay], {
            stroke: '#FF6B35', strokeWidth: 0.5,
            selectable: false, evented: false, _notchMark: true
        });
        const line2 = new fabric.Line([e2x, e2y, ax, ay], {
            stroke: '#FF6B35', strokeWidth: 0.5,
            selectable: false, evented: false, _notchMark: true
        });

        canvas.add(line1);
        canvas.add(line2);
        canvas.bringToFront(line1);
        canvas.bringToFront(line2);
        notchMarkObjects[shapeName].push(line1, line2);
    });

    canvas.renderAll();
}

// Remove all V mark objects for a shape from the canvas
function clearNotchMarksForShape(shapeName) {
    if (notchMarkObjects[shapeName]) {
        notchMarkObjects[shapeName].forEach(obj => canvas.remove(obj));
        notchMarkObjects[shapeName] = [];
    }
}

// Redraw all notch marks (e.g. after canvas resize / redrawAllShapes)
function redrawAllNotchMarks() {
    Object.keys(shapeNotches).forEach(shapeName => drawNotchMarksForShape(shapeName));
}

// Return notch geometry (mm coords) for all active notches — used by Python for G-code
function getNotches() {
    const result = {};
    Object.keys(shapeNotches).forEach(shapeName => {
        const notches = shapeNotches[shapeName];
        if (!notches || notches.size === 0) return;

        const data = shapeData[shapeName];
        if (!data || !data.originalMmPoints) return;

        result[shapeName] = [];
        notches.forEach((nodeKey) => {
            const geom = computeNotchGeometry(data.originalMmPoints, nodeKey);
            result[shapeName].push({
                nodeIdx: nodeKey.edgeIdx,
                apex: geom.apex,
                e1: geom.e1,
                e2: geom.e2
            });
        });
    });
    return result;
}

// Reset viewport zoom/pan to default (fit-to-canvas view)
function resetZoom() {
    canvas.setViewportTransform([1, 0, 0, 1, 0, 0]);
    viewZoom = 1;
    canvas.requestRenderAll();
}

// Apply notch hints from the DXF file (POINT entities on the NOTCH layer).
// Each hint is an [x, y] mm coordinate. We project it onto the nearest edge
// segment of the nearest shape and activate a notch at that exact location.
function addNotchHints(hints) {
    if (!hints || hints.length === 0) return;

    hints.forEach(([hx, hy]) => {
        let bestShape = null, bestEdgeIdx = -1, bestX = 0, bestY = 0, bestDist = Infinity;

        Object.keys(shapeData).forEach(shapeName => {
            const pts = shapeData[shapeName] && shapeData[shapeName].originalMmPoints;
            if (!pts) return;
            const n = pts.length;
            for (let i = 0; i < n; i++) {
                const j = (i + 1) % n;
                const ax = pts[i][0], ay = pts[i][1];
                const bx = pts[j][0], by = pts[j][1];
                const dx = bx - ax, dy = by - ay;
                const lenSq = dx*dx + dy*dy;
                if (lenSq < 1e-18) continue;
                let t = ((hx - ax)*dx + (hy - ay)*dy) / lenSq;
                t = Math.max(0, Math.min(1, t));
                const qx = ax + t*dx, qy = ay + t*dy;
                const d = (hx - qx)**2 + (hy - qy)**2;
                if (d < bestDist) {
                    bestDist = d;
                    bestShape = shapeName;
                    bestEdgeIdx = i;
                    bestX = qx;
                    bestY = qy;
                }
            }
        });

        if (bestShape && bestEdgeIdx >= 0) {
            const nodeKey = { edgeIdx: bestEdgeIdx, x: bestX, y: bestY };
            if (!shapeNotches[bestShape]) shapeNotches[bestShape] = new Map();
            shapeNotches[bestShape].set(bestEdgeIdx, nodeKey);
            drawNotchMarksForShape(bestShape);
            console.log(`Notch hint (${hx.toFixed(1)}, ${hy.toFixed(1)}) → ${bestShape} edge ${bestEdgeIdx} @ (${bestX.toFixed(1)}, ${bestY.toFixed(1)})`);
        }
    });

    if (notchMode) showNotchNodes();
    canvas.requestRenderAll();
}

// Export functions for use from Python
window.toolpathCanvas = {
    init: initCanvas,
    addShape: addShape,
    clearShapes: clearShapes,
    getPositions: getShapePositions,
    resize: resizeCanvas,
    // Transform tools
    mirrorX: mirrorX,
    mirrorY: mirrorY,
    alignCentersVertical: alignCentersVertical,
    alignCentersHorizontal: alignCentersHorizontal,
    distributeHorizontally: distributeHorizontally,
    distributeVertically: distributeVertically,
    rotate90: rotate90,
    rotateByDegrees: rotateByDegrees,
    scaleShape: scaleShape,
    // Position tools
    moveToOrigin: moveToOrigin,
    centerOnBed: centerOnBed,
    // Pattern tools
    linearArray: linearArray,
    gridArray: gridArray,
    mirrorCopy: mirrorCopy,
    nestShapes: nestShapes,
    // Utility
    duplicate: duplicateShape,
    deleteShape: deleteShape,
    selectAll: selectAll,
    copyShape: copyShape,
    pasteShape: pasteShape,
    undo: undo,
    saveUndoState: saveUndoState,
    // Save/Load
    saveCanvasState: saveCanvasState,
    loadCanvasState: loadCanvasState,
    getCanvasData: getCanvasData,
    // Notch tool
    setNotchMode: setNotchMode,
    getNotches: getNotches,
    // Rulers
    getRulerBounds: getRulerBounds,
    // Units
    setUnits: setUnits,
    // Zoom
    resetZoom: resetZoom,
    // Toolpath visualization
    showToolpath: showToolpath,
    clearToolpath: clearToolpath,
    isToolpathLocked: isToolpathLocked,
    updateToolhead: updateToolhead
};

// ============ UNITS TOGGLE ============

function setUnits(unit) {
    currentUnit = unit;
    drawGrid();
    drawRulers();
    // Send grid lines to back so shapes remain on top
    gridLines.forEach(line => canvas.sendToBack(line));
    if (workAreaRect) canvas.sendToBack(workAreaRect);
    canvas.renderAll();
}

// ============ TOOLPATH VISUALIZATION ============

// Check if canvas is locked due to toolpath display
function isToolpathLocked() {
    return toolpathLocked;
}

// Lock canvas interactivity - shapes cannot be moved/modified
function lockCanvas() {
    toolpathLocked = true;
    
    // Hide and disable all shapes
    Object.values(shapes).forEach(shape => {
        shape.selectable = false;
        shape.evented = false;
        shape.hasControls = false;
        shape.hasBorders = false;
        shape.visible = false;  // Hide shapes when toolpath is shown
    });
    
    canvas.discardActiveObject();
    canvas.selection = false;
    canvas.renderAll();
    console.log('Canvas locked for toolpath preview');
}

// Unlock canvas interactivity - shapes can be moved again
function unlockCanvas() {
    toolpathLocked = false;
    
    // Re-enable selection and movement for all shapes, and show them again
    Object.values(shapes).forEach(shape => {
        shape.selectable = true;
        shape.evented = true;
        shape.hasControls = true;
        shape.hasBorders = true;
        shape.visible = true;  // Show shapes again
    });
    
    canvas.selection = true;
    canvas.renderAll();
    console.log('Canvas unlocked');
}

// Clear toolpath visualization
function clearToolpath() {
    // Remove all toolpath visualization objects
    toolpathObjects.forEach(obj => {
        canvas.remove(obj);
    });
    toolpathObjects = [];
    
    // Unlock canvas
    unlockCanvas();
    
    canvas.renderAll();
    console.log('Toolpath cleared');
    return true;
}

// Show toolpath visualization from toolpath data
// toolpathData format: { shapes: { shapeName: { segments: [{x1,y1,x2,y2,angle,isCorner,cornerAngle},...] } } }
function showToolpath(toolpathData) {
    if (!toolpathData || !toolpathData.shapes) {
        console.error('Invalid toolpath data');
        return false;
    }
    
    // Clear any existing toolpath
    clearToolpathObjects();

    // Exit notch mode when toolpath is generated
    if (notchMode) {
        disableNotchMode();
        // Keep shapes unselectable — lockCanvas() will enforce this
    }

    // Lock the canvas (hides Polyline shapes)
    lockCanvas();

    const TOOLPATH_COLOR = '#3399FF';  // Blue for toolhead path
    const ARROW_COLOR = '#3399FF';     // Blue for orientation arrows
    const RAPID_COLOR = '#FF00FF';     // Magenta for rapid/jog moves between shapes
    const START_COLOR = '#00FF00';     // Green for start point
    const END_COLOR = '#FF6600';       // Orange for end point

    // Draw shape outlines as fabric.Line objects (same renderer as toolpath lines,
    // guaranteeing pixel-perfect alignment with the toolpath).
    for (const [shapeName, data] of Object.entries(shapeData)) {
        if (!data || !data.originalMmPoints) continue;
        const pts = data.originalMmPoints;
        const shapeColor = (shapes[shapeName] && shapes[shapeName].stroke) || '#42A5F5';
        for (let i = 0; i < pts.length - 1; i++) {
            const outlineLine = new fabric.Line(
                [toCanvasX(pts[i][0]), toCanvasY(pts[i][1]),
                 toCanvasX(pts[i+1][0]), toCanvasY(pts[i+1][1])],
                {
                    stroke: shapeColor,
                    strokeWidth: 1,
                    opacity: 0.5,
                    selectable: false,
                    evented: false
                }
            );
            canvas.add(outlineLine);
            toolpathObjects.push(outlineLine);
        }
    }

    // Track position across all shapes for rapid moves between them
    let globalPrevEndX = null;
    let globalPrevEndY = null;
    let globalStartX = null;
    let globalStartY = null;
    
    // Draw toolpath for each shape
    let shapeIndex = 0;
    for (const [shapeName, shapeToolpath] of Object.entries(toolpathData.shapes)) {
        if (!shapeToolpath.segments) continue;
        
        const segments = shapeToolpath.segments;
        
        for (let i = 0; i < segments.length; i++) {
            const seg = segments[i];
            
            // Track the very first point
            if (globalStartX === null) {
                globalStartX = seg.x1;
                globalStartY = seg.y1;
            }
            
            // Draw rapid move to start of this segment if there's a gap from previous position
            if (globalPrevEndX !== null && (Math.abs(seg.x1 - globalPrevEndX) > 0.1 || Math.abs(seg.y1 - globalPrevEndY) > 0.1)) {
                // Draw rapid (dashed) line from previous end to this start
                const rapidLine = new fabric.Line(
                    [toCanvasX(globalPrevEndX), toCanvasY(globalPrevEndY), toCanvasX(seg.x1), toCanvasY(seg.y1)],
                    {
                        stroke: RAPID_COLOR,
                        strokeWidth: 2,
                        strokeDashArray: [8, 4],
                        selectable: false,
                        evented: false
                    }
                );
                canvas.add(rapidLine);
                toolpathObjects.push(rapidLine);
            }
            
            // Draw the cutting line
            const cutLine = new fabric.Line(
                [toCanvasX(seg.x1), toCanvasY(seg.y1), toCanvasX(seg.x2), toCanvasY(seg.y2)],
                {
                    stroke: TOOLPATH_COLOR,
                    strokeWidth: 1,
                    opacity: 0.7,
                    selectable: false,
                    evented: false
                }
            );
            canvas.add(cutLine);
            toolpathObjects.push(cutLine);
            
            // Draw direction arrow centered on segment midpoint (no shaft)
            // Show arrows every ~20 segments to avoid clutter
            if (i % 20 === 0 && seg.angle !== undefined) {
                const midX = (seg.x1 + seg.x2) / 2;
                const midY = (seg.y1 + seg.y2) / 2;
                
                // Calculate direction from segment
                const dx = seg.x2 - seg.x1;
                const dy = seg.y2 - seg.y1;
                const segLength = Math.sqrt(dx * dx + dy * dy);
                
                if (segLength > 0.5) {  // Only draw if segment is long enough
                    const headSize = 5 / scale;  // arrow half-length in mm
                    const nx = dx / segLength;
                    const ny = dy / segLength;
                    const perpX = -ny;
                    const perpY = nx;
                    
                    // Tip and base centered on midpoint
                    const tipX  = midX + nx * headSize * 0.5;
                    const tipY  = midY + ny * headSize * 0.5;
                    const baseX = midX - nx * headSize * 0.5;
                    const baseY = midY - ny * headSize * 0.5;
                    
                    const headPoints = [
                        { x: toCanvasX(tipX)  + 0.5, y: toCanvasY(tipY)  + 0.5 },
                        { x: toCanvasX(baseX + perpX * headSize * 0.4) + 0.5,
                          y: toCanvasY(baseY + perpY * headSize * 0.4) + 0.5 },
                        { x: toCanvasX(baseX - perpX * headSize * 0.4) + 0.5,
                          y: toCanvasY(baseY - perpY * headSize * 0.4) + 0.5 }
                    ];
                    
                    const arrowHead = new fabric.Polygon(headPoints, {
                        fill: ARROW_COLOR,
                        stroke: ARROW_COLOR,
                        strokeWidth: 1,
                        selectable: false,
                        evented: false
                    });
                    canvas.add(arrowHead);
                    toolpathObjects.push(arrowHead);
                }
            }
            
            // Draw small circle at corners
            if (seg.isCorner) {
                const cornerMarker = new fabric.Circle({
                    left: toCanvasX(seg.x1) - 4,
                    top: toCanvasY(seg.y1) - 4,
                    radius: 4,
                    fill: '#FF6600',
                    stroke: '#FF6600',
                    strokeWidth: 1,
                    selectable: false,
                    evented: false
                });
                canvas.add(cornerMarker);
                toolpathObjects.push(cornerMarker);
            }
            
            // Update global position tracking
            globalPrevEndX = seg.x2;
            globalPrevEndY = seg.y2;
        }
        
        shapeIndex++;
    }
    
    // Draw START label at the beginning of toolpath
    if (globalStartX !== null) {
        const startLabel = new fabric.Text('START', {
            left: toCanvasX(globalStartX) + 8,
            top: toCanvasY(globalStartY) - 6,
            fontSize: 12,
            fill: START_COLOR,
            fontFamily: 'Roboto, sans-serif',
            fontWeight: 'bold',
            selectable: false,
            evented: false
        });
        canvas.add(startLabel);
        toolpathObjects.push(startLabel);
    }
    
    // Draw END label at the end of toolpath
    if (globalPrevEndX !== null) {
        const endLabel = new fabric.Text('END', {
            left: toCanvasX(globalPrevEndX) + 8,
            top: toCanvasY(globalPrevEndY) - 6,
            fontSize: 12,
            fill: END_COLOR,
            fontFamily: 'Roboto, sans-serif',
            fontWeight: 'bold',
            selectable: false,
            evented: false
        });
        canvas.add(endLabel);
        toolpathObjects.push(endLabel);
    }
    
    // Draw corner count in the center of each shape
    for (const [shapeName, shapeToolpath] of Object.entries(toolpathData.shapes)) {
        if (shapeToolpath.cornerCount !== undefined && shapeToolpath.centerX !== undefined) {
            const cornerLabel = new fabric.Text(String(shapeToolpath.cornerCount), {
                left: toCanvasX(shapeToolpath.centerX),
                top: toCanvasY(shapeToolpath.centerY),
                fontSize: 16,
                fill: '#FFFFFF',
                fontFamily: 'Roboto, sans-serif',
                fontWeight: 'bold',
                originX: 'center',
                originY: 'center',
                selectable: false,
                evented: false
            });
            canvas.add(cornerLabel);
            toolpathObjects.push(cornerLabel);
        }
    }
    
    canvas.renderAll();
    console.log('Toolpath displayed with', toolpathObjects.length, 'objects');
    return true;
}

// Helper to clear only toolpath objects without unlocking
function clearToolpathObjects() {
    toolpathObjects.forEach(obj => {
        canvas.remove(obj);
    });
    toolpathObjects = [];
}

// Update toolhead position indicator in realtime
function updateToolhead(x, y) {
    if (!canvas) return;
    
    const TOOLHEAD_COLOR = '#00BFFF';  // Bright blue for toolhead
    const TOOLHEAD_RADIUS = 8;
    
    // Calculate canvas position
    const canvasX = toCanvasX(x);
    const canvasY = toCanvasY(y);
    
    if (toolheadIndicator) {
        // Update existing indicator position
        toolheadIndicator.set({
            left: canvasX - TOOLHEAD_RADIUS,
            top: canvasY - TOOLHEAD_RADIUS
        });
        toolheadIndicator.setCoords();
    } else {
        // Create new toolhead indicator
        toolheadIndicator = new fabric.Circle({
            left: canvasX - TOOLHEAD_RADIUS,
            top: canvasY - TOOLHEAD_RADIUS,
            radius: TOOLHEAD_RADIUS,
            fill: TOOLHEAD_COLOR,
            stroke: '#FFFFFF',
            strokeWidth: 2,
            selectable: false,
            evented: false,
            opacity: 0.9
        });
        canvas.add(toolheadIndicator);
    }
    
    // Bring toolhead to front
    canvas.bringToFront(toolheadIndicator);
    canvas.renderAll();
}
