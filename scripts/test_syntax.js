(function() {
  fetch('url').then(function(r) { return r.json(); }).then(function(d) {
    if (!d.ok) return;
    fetch('url2').then(function(r) { return r.json(); }).then(function(pd) {
      if (pd.ok) console.log('ok');
    }).catch(function() {});
      setTimeout(function() {
        console.log('test');
      }, 1000);
    }
  }).catch(function(e) {
    console.log(e);
  });
})();
