// Fabric.js Canvas for interactive toolpath editing
let canvas = null;
let shapes = {};  // Store shape objects by name
let gridLines = [];
let workAreaRect = null;

// Canvas dimensions and scale
const WORK_WIDTH = 1365;  // mm
const WORK_HEIGHT = 875;  // mm
let scale = 1;
let canvasWidth = 800;
let canvasHeight = 500;

// Colors for shapes
const SHAPE_COLORS = ['#2196F3', '#4CAF50', '#FF9800', '#E91E63', '#9C27B0', '#00BCD4'];

function initCanvas(elementId) {
    // Get canvas element and container
    const canvasEl = document.getElementById(elementId);
    if (!canvasEl) {
        console.error('Canvas element not found:', elementId);
        return;
    }
    
    const container = document.getElementById('canvas-container') || canvasEl.parentElement;
    canvasWidth = container.clientWidth || 800;
    canvasHeight = container.clientHeight || 400;
    
    // Ensure minimum size
    if (canvasWidth < 100) canvasWidth = 800;
    if (canvasHeight < 100) canvasHeight = 400;
    
    console.log('Container size:', canvasWidth, 'x', canvasHeight);
    
    // Calculate scale to fit work area in canvas
    const scaleX = canvasWidth / WORK_WIDTH;
    const scaleY = canvasHeight / WORK_HEIGHT;
    scale = Math.min(scaleX, scaleY) * 0.95;  // 95% to leave some margin
    
    // Create Fabric canvas
    canvas = new fabric.Canvas(elementId, {
        width: canvasWidth,
        height: canvasHeight,
        backgroundColor: '#FAFAFA',
        selection: true,
        preserveObjectStacking: true
    });
    
    // Draw grid and work area
    drawGrid();
    drawWorkArea();
    
    // Handle object movement
    canvas.on('object:moved', onShapeMoved);
    canvas.on('object:modified', onShapeMoved);
    
    canvas.renderAll();
    console.log('Canvas initialized:', canvasWidth, 'x', canvasHeight, 'scale:', scale);
}

function toCanvasX(mmX) {
    // Convert mm to canvas coordinates (with offset to center)
    const offsetX = (canvasWidth - WORK_WIDTH * scale) / 2;
    return mmX * scale + offsetX;
}

function toCanvasY(mmY) {
    // Convert mm to canvas coordinates (Y is flipped, with offset to center)
    const offsetY = (canvasHeight - WORK_HEIGHT * scale) / 2;
    return canvasHeight - (mmY * scale + offsetY);
}

function fromCanvasX(canvasX) {
    const offsetX = (canvasWidth - WORK_WIDTH * scale) / 2;
    return (canvasX - offsetX) / scale;
}

function fromCanvasY(canvasY) {
    const offsetY = (canvasHeight - WORK_HEIGHT * scale) / 2;
    return (canvasHeight - canvasY - offsetY) / scale;
}

function drawGrid() {
    // Remove old grid lines
    gridLines.forEach(line => canvas.remove(line));
    gridLines = [];
    
    const gridSpacing = 35;  // mm
    const gridColor = '#E0E0E0';
    
    // Vertical lines
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
    
    // Horizontal lines
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
    
    // Send grid to back
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
        stroke: '#BDBDBD',
        strokeWidth: 2,
        selectable: false,
        evented: false
    });
    canvas.add(workAreaRect);
    canvas.sendToBack(workAreaRect);
}

function clearShapes() {
    // Remove all shape objects
    Object.values(shapes).forEach(shape => {
        canvas.remove(shape);
    });
    shapes = {};
    canvas.renderAll();
}

function addShape(name, points, colorIndex) {
    if (!canvas || !points || points.length < 2) return;
    
    // Convert points to canvas coordinates
    const canvasPoints = points.map(p => ({
        x: toCanvasX(p[0]),
        y: toCanvasY(p[1])
    }));
    
    // Create polyline path
    const color = SHAPE_COLORS[colorIndex % SHAPE_COLORS.length];
    
    const polyline = new fabric.Polyline(canvasPoints, {
        fill: 'transparent',
        stroke: color,
        strokeWidth: 2,
        selectable: true,
        hasControls: true,
        hasBorders: true,
        lockRotation: true,
        lockScalingX: true,
        lockScalingY: true,
        cornerColor: color,
        borderColor: color,
        cornerSize: 8,
        transparentCorners: false,
        shapeName: name,  // Custom property to identify shape
        originalPoints: points  // Store original mm coordinates
    });
    
    shapes[name] = polyline;
    canvas.add(polyline);
    canvas.renderAll();
    
    console.log('Added shape:', name, 'with', points.length, 'points');
}

function onShapeMoved(e) {
    const obj = e.target;
    if (!obj || !obj.shapeName) return;
    
    // Calculate the offset in mm
    const offsetX = fromCanvasX(obj.left) - fromCanvasX(obj.left - (obj.left - obj.oCoords.tl.x));
    const offsetY = fromCanvasY(obj.top) - fromCanvasY(obj.top - (obj.top - obj.oCoords.tl.y));
    
    // Get new position of first point
    const matrix = obj.calcTransformMatrix();
    const points = obj.points;
    
    // Calculate new points in mm
    const newPoints = points.map(p => {
        const transformed = fabric.util.transformPoint(p, matrix);
        return [fromCanvasX(transformed.x), fromCanvasY(transformed.y)];
    });
    
    // Send update to Python backend using NiceGUI's emitEvent
    const updateData = {
        shapeName: obj.shapeName,
        newPoints: newPoints
    };
    
    // Use NiceGUI's built-in emitEvent function
    if (typeof emitEvent === 'function') {
        emitEvent('shape_moved', updateData);
    } else {
        console.warn('emitEvent not available yet');
    }
    
    console.log('Shape moved:', obj.shapeName, 'new bounds:', 
        Math.min(...newPoints.map(p => p[0])).toFixed(1), '-',
        Math.max(...newPoints.map(p => p[0])).toFixed(1), 'x',
        Math.min(...newPoints.map(p => p[1])).toFixed(1), '-',
        Math.max(...newPoints.map(p => p[1])).toFixed(1));
}

function getShapePositions() {
    // Return current positions of all shapes in mm
    const positions = {};
    
    Object.entries(shapes).forEach(([name, obj]) => {
        const matrix = obj.calcTransformMatrix();
        const points = obj.points;
        
        positions[name] = points.map(p => {
            const transformed = fabric.util.transformPoint(p, matrix);
            return [fromCanvasX(transformed.x), fromCanvasY(transformed.y)];
        });
    });
    
    return positions;
}

// Resize handler
function resizeCanvas() {
    if (!canvas) return;
    
    const container = canvas.wrapperEl.parentElement;
    canvasWidth = container.clientWidth || 800;
    canvasHeight = container.clientHeight || 500;
    
    const scaleX = canvasWidth / WORK_WIDTH;
    const scaleY = canvasHeight / WORK_HEIGHT;
    scale = Math.min(scaleX, scaleY) * 0.95;
    
    canvas.setWidth(canvasWidth);
    canvas.setHeight(canvasHeight);
    
    // Redraw grid and work area
    drawGrid();
    drawWorkArea();
    
    // TODO: Rescale existing shapes
    canvas.renderAll();
}

// Export functions for use from Python
window.toolpathCanvas = {
    init: initCanvas,
    addShape: addShape,
    clearShapes: clearShapes,
    getPositions: getShapePositions,
    resize: resizeCanvas
};
