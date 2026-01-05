// Fabric.js Canvas for interactive toolpath editing
let canvas = null;
let shapes = {};  // Store shape objects by name
let shapeData = {};  // Store original mm points and initial positions separately
let gridLines = [];
let workAreaRect = null;

// Undo stack
let undoStack = [];
const MAX_UNDO = 50;

// Canvas dimensions and scale
const WORK_WIDTH = 1375;  // mm
const WORK_HEIGHT = 875;  // mm
const CANVAS_PADDING = 30;  // px - absolute padding around work area
let scale = 1;
let canvasWidth = 800;
let canvasHeight = 500;

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
                strokeWidth: 2,
                selectable: true,
                hasControls: true,
                hasBorders: true,
                lockRotation: true,
                lockScalingX: true,
                lockScalingY: true,
                shapeName: shapeName
            });
            
            shapes[shapeName] = newShape;
            canvas.add(newShape);
            
            // Update initial position
            data.initialLeft = newShape.left;
            data.initialTop = newShape.top;
        }
    });
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
    
    // Save undo state before any transform starts
    canvas.on('mouse:down', function(e) {
        if (e.target && e.target.shapeName) {
            saveUndoState();
        }
    });
    
    // Constrain shapes to work area during drag
    canvas.on('object:moving', onShapeMoving);
    
    // Handle object movement and transforms
    canvas.on('object:moved', onShapeMoved);
    canvas.on('object:scaled', onShapeScaled);
    canvas.on('object:rotated', onShapeRotated);
    canvas.on('object:modified', onShapeModified);
    
    // Add resize listeners
    window.addEventListener('resize', updateCanvasSize);
    
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
    
    // GCD of 1375 and 875 is 125 - use for major grid
    const majorSpacing = 125;  // mm - major grid lines
    const minorSpacing = 25;   // mm - minor grid lines
    
    const majorGridColor = GRID_COLOR;
    const minorGridColor = '#2a2a2a';  // fainter for minor lines
    const labelColor = '#888888';
    
    // Draw minor vertical lines first (so major lines are on top)
    for (let x = 0; x <= WORK_WIDTH; x += minorSpacing) {
        if (x % majorSpacing === 0) continue;  // Skip major line positions
        const line = new fabric.Line([toCanvasX(x), toCanvasY(0), toCanvasX(x), toCanvasY(WORK_HEIGHT)], {
            stroke: minorGridColor,
            strokeWidth: 1,
            selectable: false,
            evented: false
        });
        gridLines.push(line);
        canvas.add(line);
    }
    
    // Draw minor horizontal lines
    for (let y = 0; y <= WORK_HEIGHT; y += minorSpacing) {
        if (y % majorSpacing === 0) continue;  // Skip major line positions
        const line = new fabric.Line([toCanvasX(0), toCanvasY(y), toCanvasX(WORK_WIDTH), toCanvasY(y)], {
            stroke: minorGridColor,
            strokeWidth: 1,
            selectable: false,
            evented: false
        });
        gridLines.push(line);
        canvas.add(line);
    }
    
    // Draw major vertical lines and X labels
    for (let x = 0; x <= WORK_WIDTH; x += majorSpacing) {
        const line = new fabric.Line([toCanvasX(x), toCanvasY(0), toCanvasX(x), toCanvasY(WORK_HEIGHT)], {
            stroke: majorGridColor,
            strokeWidth: 1,
            selectable: false,
            evented: false
        });
        gridLines.push(line);
        canvas.add(line);
        
        // X axis label (at bottom)
        const label = new fabric.Text(Math.round(x).toString(), {
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
    
    // Draw major horizontal lines and Y labels
    for (let y = 0; y <= WORK_HEIGHT; y += majorSpacing) {
        const line = new fabric.Line([toCanvasX(0), toCanvasY(y), toCanvasX(WORK_WIDTH), toCanvasY(y)], {
            stroke: majorGridColor,
            strokeWidth: 1,
            selectable: false,
            evented: false
        });
        gridLines.push(line);
        canvas.add(line);
        
        // Y axis label (at left)
        const label = new fabric.Text(Math.round(y).toString(), {
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

// Helper: Constrain points to work area bounds
function constrainShapeToWorkArea(points) {
    const xVals = points.map(p => p[0]);
    const yVals = points.map(p => p[1]);
    const minX = Math.min(...xVals);
    const maxX = Math.max(...xVals);
    const minY = Math.min(...yVals);
    const maxY = Math.max(...yVals);
    
    let offsetX = 0, offsetY = 0;
    if (minX < 0) offsetX = -minX;
    else if (maxX > WORK_WIDTH) offsetX = WORK_WIDTH - maxX;
    if (minY < 0) offsetY = -minY;
    else if (maxY > WORK_HEIGHT) offsetY = WORK_HEIGHT - maxY;
    
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
    
    // Also remove any other objects that aren't grid/work area
    const objectsToRemove = canvas.getObjects().filter(obj => 
        obj !== workAreaRect && !gridLines.includes(obj) && !axisLabels.includes(obj)
    );
    objectsToRemove.forEach(obj => canvas.remove(obj));
    
    canvas.discardActiveObject();
    canvas.renderAll();
    console.log('Canvas cleared');
}

function addShape(name, points, colorIndexOrColor) {
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
        initialLeft: null,
        initialTop: null
    };
    
    // Constrain shape to work area bounds if needed
    if (minX < 0 || maxX > WORK_WIDTH || minY < 0 || maxY > WORK_HEIGHT) {
        let offsetX = 0, offsetY = 0;
        if (minX < 0) offsetX = -minX;
        else if (maxX > WORK_WIDTH) offsetX = WORK_WIDTH - maxX;
        if (minY < 0) offsetY = -minY;
        else if (maxY > WORK_HEIGHT) offsetY = WORK_HEIGHT - maxY;
        
        // Apply offset to constrain within bounds
        shapeData[name].originalMmPoints = points.map(p => [p[0] + offsetX, p[1] + offsetY]);
        points = shapeData[name].originalMmPoints;
        console.log('Shape constrained to work area:', name);
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
        strokeWidth: 2,
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
        shapeName: name
    });
    
    shapes[name] = polyline;
    canvas.add(polyline);
    canvas.bringToFront(polyline);
    canvas.setActiveObject(polyline);
    canvas.renderAll();
    
    // Store initial left/top position AFTER adding to canvas
    shapeData[name].initialLeft = polyline.left;
    shapeData[name].initialTop = polyline.top;
    
    console.log('addShape stored:', name,
        'initialLeft:', polyline.left.toFixed(1),
        'initialTop:', polyline.top.toFixed(1));
}

// Constrain shape to work area during drag (real-time)
function onShapeMoving(e) {
    const obj = e.target;
    if (!obj || !obj.shapeName) return;
    
    // Use the work area rect bounds directly
    if (!workAreaRect) return;
    
    // Get the bounding box of the shape
    const bound = obj.getBoundingRect(true, true); // absolute coords, skip transform
    const work = workAreaRect.getBoundingRect();
    
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

function onShapeMoved(e) {
    const obj = e.target;
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
    else if (maxX > WORK_WIDTH) constrainX = WORK_WIDTH - maxX;
    if (minY < 0) constrainY = -minY;
    else if (maxY > WORK_HEIGHT) constrainY = WORK_HEIGHT - maxY;
    
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
    
    // Log result
    const newX = newPoints.map(p => p[0]);
    const newY = newPoints.map(p => p[1]);
    console.log('Shape moved:', name, 
        'new mm X(' + Math.min(...newX).toFixed(1) + '-' + Math.max(...newX).toFixed(1) + ')',
        'Y(' + Math.min(...newY).toFixed(1) + '-' + Math.max(...newY).toFixed(1) + ')');
    
    // Send update to Python backend
    if (typeof emitEvent === 'function') {
        emitEvent('shape_moved', {
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
    
    Object.keys(shapeData).forEach(name => {
        const data = shapeData[name];
        if (data && data.originalMmPoints) {
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
    
    // Discard selection first (important for multi-select)
    canvas.discardActiveObject();
    
    const deletedNames = [];
    selectedShapes.forEach(shape => {
        const name = shape.shapeName;
        canvas.remove(shape);
        delete shapes[name];
        delete shapeData[name];
        deletedNames.push(name);
    });
    
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

function nestShapes(keepOrientation = true, spacing = 5) {
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
        
        const bbox = getPolygonBounds(simplified);
        const normalizedSimple = simplified.map(p => [p[0] - bbox.minX, p[1] - bbox.minY]);
        const normalizedFull = points.map(p => [p[0] - bbox.minX, p[1] - bbox.minY]);
        
        return {
            name: name,
            simplePoints: normalizedSimple,
            fullPoints: normalizedFull,
            width: bbox.maxX - bbox.minX,
            height: bbox.maxY - bbox.minY,
            area: (bbox.maxX - bbox.minX) * (bbox.maxY - bbox.minY),
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
                sheetWidth: WORK_WIDTH,
                sheetHeight: WORK_HEIGHT,
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
            const bbox = getPolygonBounds(simple);
            const w = bbox.maxX, h = bbox.maxY;
            
            // Generate candidates from placed shape vertices
            const candidates = generateCandidates(placedShapes, w, h, spacing);
            
            for (const pos of candidates) {
                if (pos.x + w > WORK_WIDTH || pos.y + h > WORK_HEIGHT) continue;
                
                const testPoly = simple.map(p => [p[0] + pos.x, p[1] + pos.y]);
                const testBBox = { minX: pos.x, maxX: pos.x + w, minY: pos.y, maxY: pos.y + h };
                
                // Early collision check before computing score
                let collides = false;
                for (const placed of placedShapes) {
                    if (!bboxOverlap(testBBox, placed.bbox, spacing)) continue;
                    if (polygonsCollide(testPoly, placed.simple, spacing)) {
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
                    bestSimple = testPoly;
                    bestFull = full.map(p => [p[0] + pos.x, p[1] + pos.y]);
                }
            }
        }
        
        if (!bestPos) {
            return { success: false, error: `Could not place: ${info.name}` };
        }
        
        placedShapes.push({
            simple: bestSimple,
            full: bestFull,
            bbox: getPolygonBounds(bestSimple),
            info: info
        });
    }
    
    // Calculate final bounds
    let maxX = 0, maxY = 0;
    for (const p of placedShapes) {
        if (p.bbox.maxX > maxX) maxX = p.bbox.maxX;
        if (p.bbox.maxY > maxY) maxY = p.bbox.maxY;
    }
    
    if (maxX > WORK_WIDTH || maxY > WORK_HEIGHT) {
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
        for (let y = 0; y <= WORK_HEIGHT - height; y += 50) {
            for (let x = 0; x <= WORK_WIDTH - width; x += 50) {
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
        strokeWidth: 2,
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
        shapeName: shapeName
    });
    
    shapes[shapeName] = newShape;
    canvas.add(newShape);
    canvas.setActiveObject(newShape);
    
    // Update initial position
    data.initialLeft = newShape.left;
    data.initialTop = newShape.top;
    
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
    const state = {};
    Object.keys(shapeData).forEach(name => {
        if (shapeData[name] && shapeData[name].originalMmPoints) {
            state[name] = {
                points: shapeData[name].originalMmPoints.map(p => [p[0], p[1]]),
                stroke: shapes[name] ? shapes[name].stroke : '#42A5F5'
            };
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
    
    const state = JSON.parse(undoStack.pop());
    
    // Clear current shapes
    Object.values(shapes).forEach(shape => canvas.remove(shape));
    shapes = {};
    shapeData = {};
    
    // Restore shapes from state
    Object.keys(state).forEach(name => {
        addShape(name, state[name].points, state[name].stroke);
    });
    
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
}

// Initialize keyboard listeners
document.addEventListener('keydown', handleKeyDown);

// Save canvas state to JSON
function saveCanvasState() {
    const state = {
        version: 1,
        timestamp: new Date().toISOString(),
        shapes: {}
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
    
    return JSON.stringify(state, null, 2);
}

// Load canvas state from JSON
function loadCanvasState(jsonString) {
    try {
        const state = JSON.parse(jsonString);
        
        if (!state.shapes) {
            throw new Error('Invalid canvas state: no shapes found');
        }
        
        // Clear existing shapes
        clearShapes();
        
        // Add each shape
        let colorIndex = 0;
        Object.keys(state.shapes).forEach(name => {
            const shapeState = state.shapes[name];
            if (shapeState.points && shapeState.points.length > 0) {
                addShape(name, shapeState.points, shapeState.color || colorIndex);
                colorIndex++;
            }
        });
        
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
                color: shape.stroke || '#42A5F5'
            };
        }
    });
    return data;
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
    getCanvasData: getCanvasData
};
