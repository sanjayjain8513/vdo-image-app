function shareOnTwitter() {
  const url = `https://twitter.com/intent/tweet?text=${encodeURIComponent(shareText)}&url=${encodeURIComponent(shareUrl)}`;
  window.open(url, '_blank', 'width=600,height=400');
  showShareFeedback('Twitter');
}

function shareOnFacebook() {
  const url = `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(shareUrl)}`;
  window.open(url, '_blank', 'width=600,height=400');
  showShareFeedback('Facebook');
}

function shareOnLinkedIn() {
  const url = `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(shareUrl)}`;
  window.open(url, '_blank', 'width=600,height=400');
  showShareFeedback('LinkedIn');
}

function shareOnWhatsApp() {
  const text = `${shareText}\n${shareUrl}`;
  const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

  if (isMobile) {
    window.location.href = `whatsapp://send?text=${encodeURIComponent(text)}`;
  } else {
    window.open(`https://web.whatsapp.com/send?text=${encodeURIComponent(text)}`, '_blank', 'width=600,height=600');
  }
  showShareFeedback('WhatsApp');
}

function shareOnInstagram() {
  const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

  if (isMobile) {
    copyToClipboard();
    showInstagramInstructions();
    setTimeout(() => {
      window.location.href = 'instagram://camera';
    }, 1000);
  } else {
    copyToClipboard();
    showInstagramDesktopInstructions();
  }
}

function copyToClipboard() {
  navigator.clipboard.writeText(shareUrl).then(function() {
    showToast('Link copied to clipboard!', 'success');
  }).catch(function() {
    const textArea = document.createElement('textarea');
    textArea.value = shareUrl;
    document.body.appendChild(textArea);
    textArea.select();
    document.execCommand('copy');
    document.body.removeChild(textArea);
    showToast('Link copied to clipboard!', 'success');
  });
}

function shareViaEmail() {
  const subject = encodeURIComponent(shareTitle);
  const body = encodeURIComponent(`${shareText}\n\n${shareUrl}`);
  window.location.href = `mailto:?subject=${subject}&body=${body}`;
}

function shareViaSMS() {
  const text = `${shareText}\n${shareUrl}`;
  const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

  if (isMobile) {
    window.location.href = `sms:?body=${encodeURIComponent(text)}`;
  } else {
    copyToClipboard();
    showToast('Link copied! SMS not available on desktop.', 'info');
  }
}

// Helper functions
function showShareFeedback(platform) {
  showToast(`Opening ${platform} share dialog...`, 'info');
}

function showToast(message, type = 'success') {
  const bgColor = type === 'success' ? 'bg-success' : type === 'info' ? 'bg-info' : 'bg-warning';
  const icon = type === 'success' ? 'fa-check' : type === 'info' ? 'fa-info' : 'fa-exclamation';

  const toast = document.createElement('div');
  toast.className = `toast align-items-center text-white ${bgColor} border-0 position-fixed`;
  toast.style.cssText = 'top: 20px; right: 20px; z-index: 9999;';
  toast.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">
        <i class="fas ${icon} me-2"></i>${message}
      </div>
    </div>
  `;
  document.body.appendChild(toast);

  setTimeout(() => {
    if (document.body.contains(toast)) {
      document.body.removeChild(toast);
    }
  }, 3000);
}

function showInstagramInstructions() {
  const modal = createInstructionModal(
    'Share on Instagram',
    `
    <div class="text-center">
      <i class="fab fa-instagram fa-3x text-danger mb-3"></i>
      <h5>Link copied to clipboard!</h5>
      <p class="mb-3">Instagram is opening... Once there:</p>
      <ol class="text-start">
        <li>Create a new Story or Post</li>
        <li>Add text or image</li>
        <li>Paste the link we copied for you</li>
        <li>Share with your followers!</li>
      </ol>
    </div>
    `,
    'Got it!'
  );
  document.body.appendChild(modal);
}

function showInstagramDesktopInstructions() {
  const modal = createInstructionModal(
    'Share on Instagram',
    `
    <div class="text-center">
      <i class="fab fa-instagram fa-3x text-danger mb-3"></i>
      <h5>Link copied to clipboard!</h5>
      <p class="mb-3">To share on Instagram:</p>
      <ol class="text-start">
        <li>Open Instagram on your phone</li>
        <li>Create a new Story or Post</li>
        <li>Add the link we copied to your clipboard</li>
        <li>Tell your followers about this cool tool!</li>
      </ol>
    </div>
    `,
    'Got it!'
  );
  document.body.appendChild(modal);
}

function createInstructionModal(title, content, buttonText) {
  const modal = document.createElement('div');
  modal.className = 'modal fade show';
  modal.style.display = 'block';
  modal.style.backgroundColor = 'rgba(0,0,0,0.5)';
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content">
        <div class="modal-header">
          <h5 class="modal-title">${title}</h5>
          <button type="button" class="btn-close" onclick="closeModal(this)"></button>
        </div>
        <div class="modal-body">
          ${content}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-primary" onclick="closeModal(this)">${buttonText}</button>
        </div>
      </div>
    </div>
  `;
  return modal;
}

function closeModal(button) {
  const modal = button.closest('.modal');
  document.body.removeChild(modal);
}
