const fs = require('fs');
const html = fs.readFileSync('d:/Users/基金监控/templates/fund_manage.html', 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
const js = m[1];
const lines = js.split('\n');

try {
  new Function(js);
  console.log('OK - no syntax errors');
} catch(e) {
  console.log('Error: ' + e.message);
  
  // Track brace/paren balance per line and find where it first goes wrong
  // A function definition at root level should start and end at the same depth
  let braceDepth = 0, parenDepth = 0;
  let inStr = false, sc = '';
  let lineIssue = -1;
  
  // Track balance at the start of each line
  // At root level (braceDepth === 0), parenDepth should be 0
  // If we return to braceDepth 0 but parenDepth > 0, we have an unmatched '('
  let lastRootParenDepth = 0;
  
  for (let i = 0; i < js.length; i++) {
    const c = js[i];
    if (inStr) {
      if (c === '\\') { i++; continue; }
      if (c === sc) inStr = false;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') { inStr = true; sc = c; continue; }
    if (c === '/' && i + 1 < js.length) {
      if (js[i+1] === '/') { while (i < js.length && js[i] !== '\n') i++; continue; }
      if (js[i+1] === '*') { i += 2; while (i < js.length && !(js[i] === '*' && js[i+1] === '/')) i++; i++; continue; }
    }
    if (c === '{') { braceDepth++; lastRootParenDepth = parenDepth; }
    if (c === '}') braceDepth--;
    if (c === '(') parenDepth++;
    if (c === ')') parenDepth--;
    
    if (c === '\n') {
      // Check if we just finished a line
      continue;
    }
    
    // Check for anomaly: braces went back to previous level but parens didn't
    // This is hard to check line by line, let's try a different approach
  }
  
  console.log('Final: brace=' + braceDepth + ' paren=' + parenDepth);
  
  // Find the LAST position where both depths were 0
  braceDepth = 0; parenDepth = 0;
  inStr = false; sc = '';
  let lastBalanced = 0;
  for (let i = 0; i < js.length; i++) {
    const c = js[i];
    if (inStr) {
      if (c === '\\') { i++; continue; }
      if (c === sc) inStr = false;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') { inStr = true; sc = c; continue; }
    if (c === '/' && i + 1 < js.length) {
      if (js[i+1] === '/') { while (i < js.length && js[i] !== '\n') i++; continue; }
      if (js[i+1] === '*') { i += 2; while (i < js.length && !(js[i] === '*' && js[i+1] === '/')) i++; i++; continue; }
    }
    if (c === '{') braceDepth++;
    if (c === '}') braceDepth--;
    if (c === '(') parenDepth++;
    if (c === ')') parenDepth--;
    if (braceDepth === 0 && parenDepth === 0) lastBalanced = i;
  }
  
  const balLine = js.substring(0, lastBalanced).split('\n').length;
  console.log('Last fully balanced position: after line ~' + balLine);
  
  // Show lines around the last balanced position
  for (let i = Math.max(0, balLine - 3); i < Math.min(lines.length, balLine + 4); i++) {
    const marker = i === balLine ? '>>> ' : '    ';
    console.log(marker + (i+1) + ': ' + lines[i].substring(0, 150));
  }
}
