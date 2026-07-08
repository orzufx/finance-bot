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

    if (tabId === 'tab-stats' && !myChart1) {
        loadStats('week');
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
            cardsList.innerHTML = '<p style="color:var(--hint-color);text-align:center;padding:20px;">Kartalar yo\'q</p>';
        } else {
            cardsList.innerHTML = '';
            cards.forEach(c => {
                // Dashboard card
                cardsList.innerHTML += `
                    <div class="card-item">
                        <div class="card-info">
                            <span class="name">🏦 ${c.name}</span>
                            ${c.number ? `<span class="num">**** ${c.number}</span>` : ''}
                        </div>
                        <span class="card-balance">${formatAmount(c.balance)}</span>
                    </div>
                `;
                // Add to payment select
                paySelect.innerHTML += `<option value="card_${c.id}">💳 ${c.name}</option>`;
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
        
        tg.HapticFeedback.notificationOccurred('success');
        
        // Reset and reload
        document.getElementById('amount').value = '';
        document.getElementById('comment').value = '';
        await loadDashboard();
        
        // Switch back to dashboard
        switchTab('tab-dashboard');
        
    } catch (err) {
        tg.HapticFeedback.notificationOccurred('error');
        tg.showAlert("Xatolik: " + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Saqlash';
    }
}
