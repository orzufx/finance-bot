const tg = window.Telegram.WebApp;
tg.expand();
tg.ready();

// Constants
const API_URL = '/api';
const HEADERS = {
    'Content-Type': 'application/json',
    'Authorization': 'tma ' + tg.initData
};

const EXPENSE_CATS = ["🛒 Korzinka", "🏪 Mini market", "🍽 Ovqatlanish", "🛍 Bozor", "💸 Qarz", "🏦 Kredit", "🚌 ATTO", "📦 Boshqa"];
const INCOME_CATS = ["💼 Oylik maosh", "🤝 Qarz", "🏦 Kredit", "📦 Boshqa"];

// State
let currentTab = 'tab-dashboard';
let cards = [];
let myChart1 = null;
let myChart2 = null;
let histMonth = (() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
})();
let histSearchTimer = null;

// Initialization
document.addEventListener('DOMContentLoaded', async () => {
    // Check if user is available
    if (!tg.initDataUnsafe || !tg.initDataUnsafe.user) {
        document.getElementById('loader').innerHTML = '<p>Telegram ichida oching.</p>';
        return;
    }
    
    document.getElementById('user-greeting').textContent = `Salom, ${tg.initDataUnsafe.user.first_name}!`;
    
    // Setup tabs
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.target));
    });

    // Setup form
    document.getElementById('add-form').addEventListener('submit', handleAddTransaction);
    document.querySelectorAll('input[name="type"]').forEach(r => {
        r.addEventListener('change', updateCategories);
    });

    // Setup stats
    document.querySelectorAll('.period-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            loadStats(e.target.dataset.period);
        });
    });

    // Setup history controls
    document.getElementById('hist-prev').addEventListener('click', () => shiftHistMonth(-1));
    document.getElementById('hist-next').addEventListener('click', () => shiftHistMonth(1));
    document.getElementById('hist-category').addEventListener('change', loadHistory);
    document.getElementById('hist-search').addEventListener('input', () => {
        clearTimeout(histSearchTimer);
        histSearchTimer = setTimeout(loadHistory, 350);
    });
    // Kategoriya filtri: harajat + kirim kategoriyalari (takrorlarsiz)
    const histCat = document.getElementById('hist-category');
    [...new Set([...EXPENSE_CATS, ...INCOME_CATS])].forEach(c => {
        const opt = document.createElement('option');
        opt.value = c;
        opt.textContent = c;
        histCat.appendChild(opt);
    });

    // Load data
    await loadDashboard();
    updateCategories();
    
    document.getElementById('loader').style.opacity = '0';
    setTimeout(() => document.getElementById('loader').style.display = 'none', 300);
});

// Format currency
function formatAmount(num) {
    return Number(num).toLocaleString('en-US').replace(/,/g, ' ') + " so'm";
}

// Tab Switching
function switchTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    const btn = document.querySelector(`.nav-btn[data-target="${tabId}"]`);
    if(btn) btn.classList.add('active');

    if (tabId === 'tab-stats') {
        const activeBtn = document.querySelector('.period-btn.active');
        loadStats(activeBtn ? activeBtn.dataset.period : 'week');
    }
    if (tabId === 'tab-history') loadHistory();
    if (tabId === 'tab-family') loadFamily();
}

// ── History ──
function shiftHistMonth(delta) {
    const [y, m] = histMonth.split('-').map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    histMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    loadHistory();
}

async function loadHistory() {
    document.getElementById('hist-month-label').textContent = histMonth;
    const cat = encodeURIComponent(document.getElementById('hist-category').value);
    const q = encodeURIComponent(document.getElementById('hist-search').value.trim());
    try {
        const res = await fetch(`${API_URL}/history?month=${histMonth}&category=${cat}&q=${q}`, { headers: HEADERS });
        if (!res.ok) throw new Error('Tarix yuklashda xatolik');
        const data = await res.json();

        document.getElementById('hist-total-income').textContent = formatAmount(data.total_kirim || 0);
        document.getElementById('hist-total-expense').textContent = formatAmount(data.total_harajat || 0);

        const list = document.getElementById('hist-list');
        list.innerHTML = '';

        if (!data.items.length) {
            const p = document.createElement('p');
            p.style.cssText = 'color:var(--hint-color);text-align:center;padding:20px;';
            p.textContent = 'Yozuv topilmadi';
            list.appendChild(p);
            return;
        }

        data.items.forEach(item => {
            const row = document.createElement('div');
            row.className = 'card-item';

            const info = document.createElement('div');
            info.className = 'card-info';
            const name = document.createElement('span');
            name.className = 'name';
            name.textContent = item.category;
            info.appendChild(name);
            const sub = document.createElement('span');
            sub.className = 'hist-sub';
            const d = item.date || '';
            const dateStr = d.length >= 16 ? `${d.slice(8, 10)}.${d.slice(5, 7)} ${d.slice(11, 16)}` : d;
            sub.textContent = dateStr + (item.comment ? ` · ${item.comment}` : '') + (item.payment ? ` · ${item.payment}` : '');
            info.appendChild(sub);
            row.appendChild(info);

            const right = document.createElement('div');
            right.style.cssText = 'display:flex;align-items:center;gap:4px;';
            const amt = document.createElement('span');
            amt.className = 'hist-amount ' + (item.type === 'kirim' ? 'income' : 'expense');
            amt.textContent = (item.type === 'kirim' ? '+' : '−') + formatAmount(item.amount);
            right.appendChild(amt);

            const del = document.createElement('button');
            del.className = 'del-btn';
            del.textContent = '🗑';
            del.addEventListener('click', () => confirmDeleteTxn(item.id));
            right.appendChild(del);
            row.appendChild(right);

            list.appendChild(row);
        });
    } catch (e) {
        console.error(e);
    }
}

function confirmDeleteTxn(id) {
    const doDelete = async () => {
        try {
            const res = await fetch(`${API_URL}/transaction/${id}`, { method: 'DELETE', headers: HEADERS });
            if (!res.ok) throw new Error('failed');
            try { tg.HapticFeedback.notificationOccurred('success'); } catch (e) {}
            await loadHistory();
            await loadDashboard();
        } catch (e) {
            tg.showAlert("O'chirishda xatolik yuz berdi");
        }
    };
    if (tg.showConfirm) {
        tg.showConfirm("Yozuv o'chirilib, balans qaytariladi. Davom etasizmi?", ok => { if (ok) doDelete(); });
    } else if (confirm("O'chirasizmi?")) {
        doDelete();
    }
}

// ── Family ──
async function loadFamily() {
    try {
        const res = await fetch(`${API_URL}/family`, { headers: HEADERS });
        if (!res.ok) throw new Error('Oila maʼlumotini yuklashda xatolik');
        const data = await res.json();

        document.getElementById('fam-grand-total').textContent = formatAmount(data.grand_total || 0);
        document.getElementById('fam-avo').textContent = formatAmount(data.avo || 0);
        document.getElementById('fam-month-income').textContent = formatAmount(data.family_month_kirim || 0);
        document.getElementById('fam-month-expense').textContent = formatAmount(data.family_month_harajat || 0);
        document.getElementById('fam-month-title').textContent = `BU OY (${data.month}) — OILA`;

        const listEl = document.getElementById('fam-users');
        listEl.innerHTML = '';
        (data.users || []).forEach(u => {
            const row = document.createElement('div');
            row.className = 'card-item';

            const info = document.createElement('div');
            info.className = 'card-info';
            const name = document.createElement('span');
            name.className = 'name';
            name.textContent = '👤 ' + u.name;
            info.appendChild(name);
            const sub = document.createElement('span');
            sub.className = 'hist-sub';
            sub.textContent = `Bu oy: +${formatAmount(u.month_kirim)} · −${formatAmount(u.month_harajat)}`;
            info.appendChild(sub);
            row.appendChild(info);

            const bal = document.createElement('span');
            bal.className = 'card-balance';
            bal.textContent = formatAmount(u.total);
            row.appendChild(bal);

            listEl.appendChild(row);
        });
    } catch (e) {
        console.error(e);
    }
}

// Update categories dropdown based on type
function updateCategories() {
    const type = document.querySelector('input[name="type"]:checked').value;
    const catSelect = document.getElementById('category');
    catSelect.innerHTML = '';
    
    const cats = type === 'harajat' ? EXPENSE_CATS : INCOME_CATS;
    cats.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c;
        opt.textContent = c;
        catSelect.appendChild(opt);
    });
}

// Load Dashboard Data
async function loadDashboard() {
    try {
        const res = await fetch(`${API_URL}/dashboard`, { headers: HEADERS });
        if(!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.error || 'Server bilan ulanishda xatolik');
        }
        const data = await res.json();
        
        // Balances
        const total = data.avo + data.naqd + data.cards.reduce((sum, c) => sum + c.balance, 0);
        document.getElementById('dash-total-balance').textContent = formatAmount(total);
        document.getElementById('dash-avo-balance').textContent = formatAmount(data.avo);
        document.getElementById('dash-naqd-balance').textContent = formatAmount(data.naqd);
        
        // Cards
        cards = data.cards;
        const cardsList = document.getElementById('dash-cards-list');
        const paySelect = document.getElementById('payment-method');
        
        // Reset card options
        Array.from(paySelect.options).forEach(o => {
            if(!o.value.includes('AVO') && !o.value.includes('Naqd')) o.remove();
        });

        if (cards.length === 0) {
            const noCards = document.createElement('p');
            noCards.style.cssText = 'color:var(--hint-color);text-align:center;padding:20px;';
            noCards.textContent = "Kartalar yo'q";
            cardsList.innerHTML = '';
            cardsList.appendChild(noCards);
        } else {
            cardsList.innerHTML = '';
            cards.forEach(c => {
                // Dashboard card
                const cardDiv = document.createElement('div');
                cardDiv.className = 'card-item';

                const infoDiv = document.createElement('div');
                infoDiv.className = 'card-info';

                const nameSpan = document.createElement('span');
                nameSpan.className = 'name';
                nameSpan.textContent = '🏦 ' + c.name;
                infoDiv.appendChild(nameSpan);

                if (c.number) {
                    const numSpan = document.createElement('span');
                    numSpan.className = 'num';
                    numSpan.textContent = '**** ' + c.number;
                    infoDiv.appendChild(numSpan);
                }

                cardDiv.appendChild(infoDiv);

                const balSpan = document.createElement('span');
                balSpan.className = 'card-balance';
                balSpan.textContent = formatAmount(c.balance);
                cardDiv.appendChild(balSpan);

                cardsList.appendChild(cardDiv);

                // Add to payment select
                const opt = document.createElement('option');
                opt.value = 'card_' + c.id;
                opt.textContent = '💳 ' + c.name;
                paySelect.appendChild(opt);
            });
        }
        
        // Today Stats
        document.getElementById('dash-today-income').textContent = formatAmount(data.today.kirim || 0);
        document.getElementById('dash-today-expense').textContent = formatAmount(data.today.harajat || 0);

    } catch (e) {
        console.error(e);
        document.getElementById('tab-dashboard').innerHTML = `
            <div style="text-align:center; padding: 40px 20px;">
                <h2 style="color: var(--expense-color); margin-bottom: 10px;">Xatolik!</h2>
                <p style="color: var(--hint-color);">${e.message}</p>
                <p style="color: var(--hint-color); margin-top: 20px;">Botga qaytib /start ni bosing va ismingizni kiritib ro'yxatdan o'ting.</p>
            </div>
        `;
    }
}

// Load Statistics
async function loadStats(period) {
    try {
        const res = await fetch(`${API_URL}/stats?period=${period}`, { headers: HEADERS });
        if(!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.error || 'Stats yuklashda xatolik');
        }
        const data = await res.json();
        
        // Bar Chart (Income vs Expense)
        const ctx1 = document.getElementById('barChart').getContext('2d');
        if(myChart1) myChart1.destroy();
        myChart1 = new Chart(ctx1, {
            type: 'bar',
            data: {
                labels: data.labels,
                datasets: [
                    { label: 'Kirim', data: data.incomes, backgroundColor: 'rgba(138, 201, 38, 0.8)', borderRadius: 4 },
                    { label: 'Harajat', data: data.expenses, backgroundColor: 'rgba(255, 89, 94, 0.8)', borderRadius: 4 }
                ]
            },
            options: {
                responsive: true,
                scales: {
                    x: { grid: { display: false } },
                    y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { display: false } }
                },
                plugins: {
                    legend: { labels: { color: '#8a96a3' } }
                }
            }
        });

        // Donut Chart (Categories)
        const ctx2 = document.getElementById('donutChart').getContext('2d');
        if(myChart2) myChart2.destroy();
        
        const catLabels = Object.keys(data.categories);
        const catData = Object.values(data.categories);
        
        myChart2 = new Chart(ctx2, {
            type: 'doughnut',
            data: {
                labels: catLabels,
                datasets: [{
                    data: catData,
                    backgroundColor: ['#ff595e', '#ffca3a', '#8ac926', '#1982c4', '#6a4c93', '#f15bb5', '#00bbf9'],
                    borderWidth: 0,
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { position: 'right', labels: { color: '#8a96a3', font: {size: 11} } }
                }
            }
        });

    } catch (e) {
        console.error(e);
    }
}

// Form Submit
async function handleAddTransaction(e) {
    e.preventDefault();
    const btn = document.getElementById('submit-btn');
    btn.disabled = true;
    btn.textContent = 'Yuklanmoqda...';
    
    const type = document.querySelector('input[name="type"]:checked').value;
    const amount = document.getElementById('amount').value;
    const paymentVal = document.getElementById('payment-method').value;
    const category = document.getElementById('category').value;
    const comment = document.getElementById('comment').value;
    
    let payment_method = paymentVal;
    let card_id = null;
    
    if (paymentVal.startsWith('card_')) {
        card_id = paymentVal.split('_')[1];
        const cardName = document.querySelector(`#payment-method option[value="${paymentVal}"]`).textContent;
        payment_method = cardName; // Save original card name to db
    }
    
    try {
        const res = await fetch(`${API_URL}/transaction`, {
            method: 'POST',
            headers: HEADERS,
            body: JSON.stringify({ type, amount, payment_method, category, comment, card_id })
        });
        
        if(!res.ok) throw new Error('Failed to save');
        
        try { tg.HapticFeedback.notificationOccurred('success'); } catch(e) {}
        
        // Reset and reload
        document.getElementById('amount').value = '';
        document.getElementById('comment').value = '';
        await loadDashboard();
        
        // Switch back to dashboard
        switchTab('tab-dashboard');
        
    } catch (err) {
        try { tg.HapticFeedback.notificationOccurred('error'); } catch(e) {}
        tg.showAlert("Xatolik: " + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Saqlash';
    }
}
