var socket = io();

// showToast: title, message, type ∈ {‘info’,‘success’,‘warning’,‘danger’}
function showToast(title, message, type='info') {
    const toastContainer = document.getElementById('toast-container');
    if (!toastContainer) return console.warn('Toast container missing');
    const toastEl = document.createElement('div');
    toastEl.className = `toast align-items-center text-bg-${type} border-0`;
    toastEl.setAttribute('role','alert');
    toastEl.innerHTML = `
      <div class="d-flex">
        <div class="toast-body">
          <strong>${title}:</strong> ${message}
        </div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>`;
    toastContainer.appendChild(toastEl);
    new bootstrap.Toast(toastEl, { delay: 5000 }).show();
}

socket.on('connect', function() {
    if (userId) {
        socket.emit('join', { room: userId.toString() });
    } else {
        console.error("User ID not set.");
    }
});

// Live tick updates
socket.on('live_tick_update', function(data) {
    console.log("Live tick update received:", data);
    var token = data.token;
    var ltp = data.ltp;
    document.querySelectorAll("[data-token='"+token+"']").forEach(function(el) {
        el.innerText = ltp;
    });
    var display = document.getElementById("priceDisplay");
    if (display) display.innerText = ltp;
});

// Order updates
socket.on("order_update", function(data) {
    console.log("Order update received:", data);

    let body, toastType;

    // 1) Wrapper‐level errors
    if (data.error) {
        body      = `<strong>Error:</strong> ${data.error}`;
        toastType = "danger";

    // 2) place_order() ACK/NOK
    } else if (data.result) {
        const res = Array.isArray(data.result) ? data.result[0] : data.result;
        if (res.stat === "Not_Ok") {
            body      = `<strong>Order Rejected:</strong> ${res.emsg || "Unknown reason"}`;
            toastType = "warning";
        } else {
            body      = `<strong>Order Placed:</strong> #${res.norenordno}`;
            toastType = "info";
        }

    // 3) single_order_history record
    } else if (data.status) {
        // 3a) Rejection
        if (data.status === "REJECTED"
         || data.st_intrn === "REJECTED"
         || (data.reporttype||"").toLowerCase() === "rejected") {
            const why = data.rejreason || "No reason provided";
            body      = `<strong>Order Rejected:</strong> ${data.reporttype||data.status} – ${why}`;
            toastType = "warning";

        // 3b) Fill event (only if fillshares > 0)
        } else {
            const filled = parseInt(data.fillshares || 0, 10);
            if (filled > 0) {
                const price = data.avgprc || data.prc || data.rprc || "0.00";
                body      = `<strong>Order Executed:</strong> ${filled} @ ${price}`;
                toastType = "success";
            } else {
                return;  // skip any other statuses
            }
        }

    // 4) Unexpected format
    } else {
        console.warn("order_update payload missing error, result, and status", data);
        body      = "Unexpected update format";
        toastType = "danger";
    }

    // Render alert
    const alertBox =
      `<div class="alert alert-${toastType} alert-dismissible fade show" role="alert">` +
        body +
        `<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>` +
      `</div>`;
    document.getElementById("notification-area")
            .insertAdjacentHTML("beforeend", alertBox);
});

// Strategy updates
socket.on("strategy_update", function(updatedState) {
    if (updatedState.error) {
      return showToast("Error", updatedState.error, "danger");
    }
    
    console.log("Received strategy update:", updatedState);

    Object.keys(updatedState).forEach(scriptName => {
        const data = updatedState[scriptName];
        // grab the <tr> once
        const row = document.querySelector(`tr[data-script-name="${scriptName}"]`);
        if (!row) return;

        // Update numeric fields (LTP, P&L etc.)...
        var ltpCell    = row.querySelector("[data-field='current_ltp']");
        var qtyCell    = row.querySelector("[data-field='purchasedQty']");
        var avgCell    = row.querySelector("[data-field='avgPrice']");
        var invCell    = row.querySelector("[data-field='investment']");
        var curCell    = row.querySelector("[data-field='currentValue']");
        var pnlCell    = row.querySelector("[data-field='pnl']");
        var pctCell    = row.querySelector("[data-field='pnlPercent']");
        if (ltpCell) ltpCell.innerText = data.current_ltp;
        var qty = parseFloat(qtyCell?.innerText) || 0;
        var avg = parseFloat(avgCell?.innerText) || 0;
        var inv = qty * avg;
        var cur = qty * data.current_ltp;
        var pnl = cur - inv;
        var pct = inv ? (pnl / inv * 100) : 0;
        if (invCell) invCell.innerText = inv.toFixed(2);
        if (curCell) curCell.innerText = cur.toFixed(2);
        if (pnlCell) pnlCell.innerText = pnl.toFixed(2);
        if (pctCell) pctCell.innerText = pct.toFixed(2) + "%";

        // Update status badge (find the <td> by its class, not by scriptName)
        const badge = row.querySelector(".status-field span.badge");
        if (!badge) return;
        const cls = data.status === "Running"   ? "bg-success"
                : data.status === "Waiting"   ? "bg-secondary"
                : data.status === "Paused"    ? "bg-warning text-dark"
                : data.status === "Failed" || data.status === "Skipped"   ? "bg-danger"
                : data.status === "Sold-out"  ? "bg-info text-dark"
                : "";
        badge.className = "badge " + cls;
        badge.innerText = data.status;

        // now handle the Retry button
        const scriptId    = row.dataset.scriptId;
        const actionsCell = row.querySelector("td:last-child");
        if (data.status === "Failed" || data.status === "Skipped") {
          // only add one
          if (actionsCell && !actionsCell.querySelector(".btn-retry")) {
            const btn = document.createElement("button");
            btn.className = "btn btn-sm btn-primary btn-retry";
            btn.dataset.scriptId   = scriptId;
            btn.dataset.scriptName = scriptName;
            btn.title               = "Retry this script";
            btn.innerText           = "Retry";
            actionsCell.appendChild(btn);
          }
        } else {
          // remove any old Retry
          const old = actionsCell.querySelector(".btn-retry");
          if (old) old.remove();
        }
    });
});

socket.on("balance_update", function(data) {
  // find and update the balance-display element
  const el = document.getElementById("balance-display");
  if (el) {
      el.textContent = `₹${data.balance}`;
  }
});

socket.on("order_skipped", function(data) {
  const msg = `Skipping order for ${data.symbol}: insufficient margin (have ${data.available.toFixed(2)}, need ${data.required.toFixed(2)})`;
  showToast("Order Skipped", msg, "warning");
});

// Retry button handler
$(document).on("click", ".btn-retry", function(e) {
    e.preventDefault();
    const btn        = $(this);
    const scriptId   = btn.data("scriptId");   // <-- was data("script-id")
    const scriptName = btn.data("scriptName"); // <-- was data("script-name")
  
    $.post(`/script/${scriptId}/retry`, {}, function(json) {
      if (json.ok) {
        const st = json.new_status;
        // now find the row by its symbol-based id and update the badge
        const $badge = $(`#row-${scriptName} .status-field span.badge`);
        const cls    = st === "Running" ? "bg-success"
                      : st === "Waiting" ? "bg-secondary"
                      : "bg-danger";
  
        $badge
          .removeClass()           // drop whatever was there
          .addClass("badge " + cls)// add the two new classes
          .text(st);               // update the label
  
        btn.remove();             // hide the Retry button once done
        showToast("Retry", `Script reset to ${st}`, "success");
      } else {
        showToast("Error", json.error || "Could not retry", "danger");
      }
    }).fail(xhr => {
      showToast("Error", xhr.responseJSON?.error || "Server error", "danger");
    });
  });  

// Toggle re-entry groups
$(document).ready(function(){
    // $('#reentry_prev_day_checkbox').change(function(){
    //     $('#reentry_prev_day_group').toggle(this.checked).find('input,select').val('');
    // });
    // $('#reentry_last_buy_checkbox').change(function(){
    //     $('#reentry_last_buy_group').toggle(this.checked).find('input').val('');
    // });
    // $('#reentry_weighted_checkbox').change(function(){
    //     $('#reentry_weighted_group').toggle(this.checked).find('input').val('');
    // });

    
    // Prev-day
    $('#reentry_prev_day_checkbox').on('change', function() {
      $('#reentry_prev_day_group').toggle(this.checked);
    });
    // Last-buy
    $('#reentry_last_buy_checkbox').on('change', function() {
      $('#reentry_last_buy_group').toggle(this.checked);
    });
    // Weighted-avg
    $('#reentry_weighted_checkbox').on('change', function() {
      $('#reentry_weighted_group').toggle(this.checked);
    });

    // Pause/Resume scripts
    $(".toggle-script-status").click(function(e){
        e.preventDefault();
        var btn = $(this), id = btn.data("scriptId");
        $.post("/toggle_script_status/" + id, {}, function(resp){
            if (resp.new_status) {
                var st = resp.new_status;
                var icon = btn.find("i");
                if (st.toLowerCase() === "paused") {
                    // now it's paused → show Resume state
                    btn.removeClass("btn-outline-warning").addClass("btn-outline-success");
                    btn.attr("title", "Resume");
                    icon.attr("class", "bi bi-play-fill");
                } else {
                    // now it's running/waiting → show Pause state
                    btn.removeClass("btn-outline-success").addClass("btn-outline-warning");
                    btn.attr("title", "Pause");
                    icon.attr("class", "bi bi-pause-fill");
                }
                // var cell = $(".status-field", "#row-" + id);
                // var badge = cell.find("span.badge");
                var row   = $('tr[data-script-id="'+ id +'"]');
                var badge = row.find(".status-field span.badge");
                var c = st==="Running"?"bg-success":st==="Waiting"?"bg-secondary":st==="Paused"?"bg-warning text-dark":"";
                badge.removeClass().addClass("badge " + c).text(st);

                // ─── Inject a Bootstrap alert on the detail page ─────────
                var flashArea = $("#flash-message-area");
                if (flashArea.length) {
                    var msg = st.toLowerCase()==="paused"
                                        ? "Strategy paused successfully."
                                        : "Strategy resumed successfully.";
                    var alertHtml = 
                        '<div class="alert alert-info alert-dismissible fade show" role="alert">' +
                            msg +
                            '<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>' +
                        '</div>';
                    flashArea.html(alertHtml);
                }
            }
        }).fail(function(){ showToast("Error","Could not toggle","danger"); });
    });

  // Enable tooltips everywhere
  var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'))
  var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
    return new bootstrap.Tooltip(tooltipTriggerEl)
  })

  // Confirm “exit” or “delete” before actually submitting the form
  $('[data-confirm]').click(function(e) {
    // get the message from the attribute
    var message = $(this).attr('data-confirm');
    // show native confirm dialog
    if (!window.confirm(message)) {
      // user cancelled: prevent the form submission
      e.preventDefault();
      return false;
    }
    // otherwise let the click go through and submit the form
  });

});

// show/hide toggle script for password eye
document.querySelectorAll('.toggle-password').forEach(btn => {
  btn.addEventListener('click', () => {
    const input = document.querySelector(btn.getAttribute('data-target'));
    const icon  = btn.querySelector('i');
    if (input.type === 'password') {
      input.type = 'text';
      icon.classList.replace('bi-eye', 'bi-eye-slash');
    } else {
      input.type = 'password';
      icon.classList.replace('bi-eye-slash', 'bi-eye');
    }
  });
});