/**
 * Story Arc Explorer & CBL Import Interaction Script
 * Modern Theme Static Asset
 */

(function(window, $) {
    'use strict';

    var MANIFEST_META_DEFAULT = {
        'marvel_secret_invasion': {
            file: '[Marvel] (2007-08) Secret Invasion (Official).cbl',
            repo: 'Bundled Example Manifest (98 Issues)',
            hash: '81f59876e25586d5462ebb45e42b1b308129bfac9a34b9c3ce21c34092fb7a50'
        },
        'batman_lonely_place_of_dying': {
            file: '[DC Comics] Batman- A Lonely Place of Dying (WEB-CBRO).cbl',
            repo: 'Bundled Example Manifest (5 Issues)',
            hash: 'c33e762620fefe249015c10d8591e40492edbfde20b47581ddd0e14f1a2f60f6'
        }
    };

    var currentToken = null;
    var currentTab = 'upload';
    var pageConfig = {};
    var searchTimer = null;
    var isRefreshingCatalog = false;

    function initConfig() {
        var cfgEl = document.getElementById('storyarc-page-config');
        if (cfgEl) {
            try {
                pageConfig = JSON.parse(cfgEl.textContent || '{}');
            } catch (e) {
                pageConfig = {};
            }
        }
        if (window.MylarStoryArcConfig) {
            pageConfig = $.extend({}, pageConfig, window.MylarStoryArcConfig);
        }
    }

    function triggerCblFilePicker(e) {
        if (e) {
            e.stopPropagation();
        }
        var fileInput = document.getElementById('cblFileInput');
        if (fileInput) {
            fileInput.value = null;
            fileInput.click();
        }
    }

    function switchImportTab(tab) {
        currentTab = tab;
        currentToken = null;
        $('#modalAlertBox').hide();
        $('#previewResultsContainer').hide();
        $('#manifestMetaBox').hide();
        $('#confirmImportBtn').hide();
        $('#fetchPreviewBtn').hide();

        $('.cbl-tab-btn').removeClass('active');
        $('.tab-content').hide();

        if (tab === 'upload') {
            $('#tabUploadBtn').addClass('active');
            $('#tabContentUpload').show();
        } else if (tab === 'staged') {
            $('#tabStagedBtn').addClass('active');
            $('#tabContentStaged').show();
            $('#fetchPreviewBtn').show().text('Validate & Preview').prop('disabled', false);
            updateManifestMeta();
        } else if (tab === 'catalog') {
            $('#tabCatalogBtn').addClass('active');
            $('#tabContentCatalog').show();
            loadCatalogStatus(true);
        }
    }

    function updateManifestMeta() {
        var metaMap = pageConfig.manifestMeta || MANIFEST_META_DEFAULT;
        var token = $('#manifestTokenSelect').val();
        var meta = metaMap[token];
        if (meta) {
            $('#metaSourceFile').text(meta.file);
            $('#metaRepo').text(meta.repo);
            $('#metaRepoRow').show();
            $('#metaHash').text(meta.hash);
            $('#manifestMetaBox').show();
        }
        $('#previewResultsContainer').hide();
        $('#modalAlertBox').hide();
        $('#confirmImportBtn').hide();
        $('#fetchPreviewBtn').show().text('Validate & Preview').prop('disabled', false);
    }

    function openCblModal() {
        $('#cblImportModal').fadeIn(150);
        switchImportTab('upload');
    }

    function closeCblModal() {
        $('#cblImportModal').fadeOut(150);
    }

    function handleFileSelected(files) {
        if (!files || files.length === 0) return;
        var file = files[0];
        var formData = new FormData();
        formData.append('cbl_file', file);

        $('#dropzoneText').html('Uploading & validating <strong>' + file.name + '</strong>...');
        $('#modalAlertBox').hide();
        $('#previewResultsContainer').hide();
        $('#confirmImportBtn').hide();

        $.ajax({
            url: 'cbl_upload',
            type: 'POST',
            data: formData,
            processData: false,
            contentType: false,
            dataType: 'json',
            success: function(resp) {
                $('#dropzoneText').html('Drag and drop a <strong>.cbl</strong> or <strong>.xml</strong> file here, or <span class="dropzone-link">browse</span>');
                if (resp.status === 'success') {
                    currentToken = resp.upload_token;
                    $('#metaSourceFile').text(resp.filename);
                    $('#metaRepoRow').hide();
                    $('#metaHash').text(resp.sha256);
                    $('#manifestMetaBox').show();
                    renderPreviewResults(resp);
                } else {
                    $('#modalAlertBox')
                        .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                        .addClass('modal-alert-error')
                        .text(resp.message || 'Validation error')
                        .show();
                }
            },
            error: function() {
                $('#dropzoneText').html('Drag and drop a <strong>.cbl</strong> or <strong>.xml</strong> file here, or <span class="dropzone-link">browse</span>');
                $('#modalAlertBox')
                    .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                    .addClass('modal-alert-error')
                    .text('Failed to upload CBL file to server.')
                    .show();
            }
        });
    }

    function fetchCblPreview() {
        var token = $('#manifestTokenSelect').val();
        if (!token) return;

        $('#fetchPreviewBtn').prop('disabled', true).text('Validating...');
        $('#modalAlertBox').hide();
        $('#previewResultsContainer').hide();

        $.ajax({
            url: 'cbl_preview',
            type: 'GET',
            data: { token: token },
            dataType: 'json',
            success: function(resp) {
                $('#fetchPreviewBtn').prop('disabled', false).text('Validate & Preview');
                if (resp.status === 'success') {
                    currentToken = token;
                    renderPreviewResults(resp);
                } else {
                    $('#modalAlertBox')
                        .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                        .addClass('modal-alert-error')
                        .text(resp.message || 'Validation error')
                        .show();
                }
            },
            error: function() {
                $('#fetchPreviewBtn').prop('disabled', false).text('Validate & Preview');
                $('#modalAlertBox')
                    .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                    .addClass('modal-alert-error')
                    .text('Failed to fetch preview from server.')
                    .show();
            }
        });
    }

    function renderPreviewResults(resp) {
        var rowsHtml = '';
        var unmatchedCount = 0;
        resp.results.forEach(function(item) {
            var statusClass = 'badge-status-unmatched';
            var rstate = item.resolution_state;
            if (rstate === 'Downloaded') statusClass = 'badge-status-downloaded';
            else if (rstate.indexOf('Monitored') !== -1 && rstate.indexOf('Unmonitored') === -1) statusClass = 'badge-status-monitored';
            else if (rstate.indexOf('Unmonitored') !== -1) statusClass = 'badge-status-unmonitored';
            else if (rstate.indexOf('Unknown') !== -1 || rstate.indexOf('Unmatched') !== -1) {
                statusClass = 'badge-status-unmatched';
                unmatchedCount++;
            }

            var cvDisplay = '—';
            if (item.cv_series_id && item.cv_issue_id) {
                cvDisplay = item.cv_series_id + ' / ' + item.cv_issue_id;
            }

            rowsHtml += '<tr>' +
                '<td><strong>#' + item.order + '</strong></td>' +
                '<td>' + (item.matched_comic_name || item.series_name || '—') + '</td>' +
                '<td>#' + (item.issue_number || '—') + '</td>' +
                '<td><code style="font-size:10px;">' + cvDisplay + '</code></td>' +
                '<td><span class="badge-status ' + statusClass + '">' + item.resolution_state + '</span></td>' +
            '</tr>';
        });
        $('#previewTableBody').html(rowsHtml);
        $('#previewResultsContainer').show();

        if (resp.is_already_imported) {
            $('#modalAlertBox')
                .removeClass('modal-alert-error modal-alert-success modal-alert-warning')
                .addClass('modal-alert-info')
                .html('<strong>Notice:</strong> This reading list is already imported on your watchlist. <a href="detailStoryArc?StoryArcID=' + resp.existing_arc_id + '" style="color:#60a5fa; text-decoration:underline;">View Story Arc</a>')
                .show();
            $('#confirmImportBtn').hide();
        } else if (unmatchedCount > 0) {
            var totalCount = resp.total_issues;
            var entryText = (unmatchedCount === 1)
                ? '1 of ' + totalCount + ' entries is unresolved and will be imported as an Unknown / Unmatched Reference.'
                : unmatchedCount + ' of ' + totalCount + ' entries are unresolved and will be imported as Unknown / Unmatched References.';
            var alertHtml = '<strong>Manifest is structurally valid.</strong> ' + entryText + ' No issue statuses will be modified.';

            $('#modalAlertBox')
                .removeClass('modal-alert-error modal-alert-info modal-alert-success')
                .addClass('modal-alert-warning')
                .html(alertHtml)
                .show();

            var btnText = (unmatchedCount === 1)
                ? 'Confirm Import with 1 Unmatched Reference'
                : 'Confirm Import with ' + unmatchedCount + ' Unmatched References';

            $('#confirmImportBtn')
                .show()
                .prop('disabled', false)
                .text(btnText);
        } else {
            var alertHtml = '<strong>Manifest is structurally valid.</strong> All ' + resp.total_issues + ' entries have authoritative ComicVine IDs. No issue statuses will be modified.';

            $('#modalAlertBox')
                .removeClass('modal-alert-error modal-alert-info modal-alert-warning')
                .addClass('modal-alert-success')
                .html(alertHtml)
                .show();

            $('#confirmImportBtn')
                .show()
                .prop('disabled', false)
                .text('Confirm & Import Story Arc');
        }

        setTimeout(function() {
            var previewEl = document.getElementById('previewResultsContainer');
            if (previewEl && typeof previewEl.scrollIntoView === 'function') {
                previewEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }, 50);
    }

    function confirmCblImport() {
        if (!currentToken) return;

        $('#confirmImportBtn').prop('disabled', true).text('Importing...');
        $('#modalAlertBox').hide();

        $.ajax({
            url: 'cbl_confirm_import',
            type: 'POST',
            data: { token: currentToken },
            dataType: 'json',
            success: function(resp) {
                if (resp.status === 'success') {
                    $('#modalAlertBox')
                        .removeClass('modal-alert-error modal-alert-warning modal-alert-info')
                        .addClass('modal-alert-success')
                        .html('<strong>Success!</strong> Story Arc imported. Redirecting...')
                        .show();
                    setTimeout(function() {
                        window.location.href = 'detailStoryArc?StoryArcID=' + resp.storyarcid;
                    }, 800);
                } else if (resp.status === 'already_imported') {
                    $('#modalAlertBox')
                        .removeClass('modal-alert-error modal-alert-success modal-alert-warning')
                        .addClass('modal-alert-info')
                        .html('<strong>Notice:</strong> ' + resp.message + ' <a href="detailStoryArc?StoryArcID=' + resp.storyarcid + '" style="color:#60a5fa; text-decoration:underline;">View Story Arc</a>')
                        .show();
                    $('#confirmImportBtn').hide();
                } else {
                    $('#confirmImportBtn').prop('disabled', false).text('Confirm & Import Story Arc');
                    $('#modalAlertBox')
                        .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                        .addClass('modal-alert-error')
                        .text(resp.message || 'Import failed.')
                        .show();
                }
            },
            error: function() {
                $('#confirmImportBtn').prop('disabled', false).text('Confirm & Import Story Arc');
                $('#modalAlertBox')
                    .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                    .addClass('modal-alert-error')
                    .text('Error communicating with server during import.')
                    .show();
            }
        });
    }

    // ==========================================
    // DieselTech Catalog Browser Integration
    // ==========================================

    function loadCatalogStatus(triggerSearchIfCached) {
        $.ajax({
            url: 'cbl_catalog_status',
            type: 'GET',
            dataType: 'json',
            success: function(status) {
                updateCatalogStatusUI(status);
                if (status.cached) {
                    $('#catalogEmptyState').hide();
                    $('#catalogSearchControls').show();
                    if (triggerSearchIfCached) {
                        handleCatalogSearchInput();
                    }
                } else {
                    $('#catalogSearchControls').hide();
                    $('#catalogEmptyState').show();
                }
            },
            error: function() {
                $('#catalogStatusDot').removeClass('active stale');
                $('#catalogStatusText').text('Unable to determine catalog status.');
            }
        });
    }

    function updateCatalogStatusUI(status) {
        var dot = $('#catalogStatusDot');
        dot.removeClass('active stale');

        if (status.cached) {
            if (status.stale) {
                dot.addClass('stale');
                $('#catalogStatusText').text('Cached (' + status.total_count + ' lists · ' + (status.commit_short || '') + ' · updated ' + status.fetched_at + ' - Stale)');
            } else {
                dot.addClass('active');
                $('#catalogStatusText').text('Cached (' + status.total_count + ' lists · ' + (status.commit_short || '') + ' · updated ' + status.fetched_at + ')');
            }
        } else {
            $('#catalogStatusText').text('No local catalog cached.');
        }
    }

    function refreshDieselTechCatalog() {
        if (isRefreshingCatalog) return;
        isRefreshingCatalog = true;

        var refreshBtn = $('#refreshCatalogBtn');
        refreshBtn.prop('disabled', true).html('Refreshing from GitHub...');
        $('#modalAlertBox').hide();

        $.ajax({
            url: 'cbl_catalog_refresh',
            type: 'POST',
            dataType: 'json',
            success: function(resp) {
                isRefreshingCatalog = false;
                refreshBtn.prop('disabled', false).html(
                    '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                    '<path d="M1 4v4h4"></path><path d="M3.51 10a5 5 0 1 0 1.1-5.5L1 8"></path></svg> Refresh DieselTech Catalog'
                );

                if (resp.status === 'success') {
                    loadCatalogStatus(true);
                } else {
                    $('#modalAlertBox')
                        .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                        .addClass('modal-alert-error')
                        .text(resp.message || 'Catalog refresh failed.')
                        .show();
                    // Still check if we have cached snapshot to browse
                    loadCatalogStatus(false);
                }
            },
            error: function() {
                isRefreshingCatalog = false;
                refreshBtn.prop('disabled', false).html(
                    '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                    '<path d="M1 4v4h4"></path><path d="M3.51 10a5 5 0 1 0 1.1-5.5L1 8"></path></svg> Refresh DieselTech Catalog'
                );
                $('#modalAlertBox')
                    .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                    .addClass('modal-alert-error')
                    .text('Network error communicating with server during catalog refresh.')
                    .show();
                loadCatalogStatus(false);
            }
        });
    }

    function handleCatalogSearchInput() {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function() {
            var q = $('#catalogSearchInput').val() || '';
            var pub = $('#catalogPublisherSelect').val() || '';

            $.ajax({
                url: 'cbl_catalog_search',
                type: 'GET',
                data: { q: q, publisher: pub, limit: 150 },
                dataType: 'json',
                success: function(resp) {
                    renderCatalogSearchResults(resp);
                }
            });
        }, 120);
    }

    function renderCatalogSearchResults(resp) {
        if (!resp || resp.status !== 'success') {
            $('#catalogTableBody').html('<tr><td colspan="4" style="text-align:center; color:#64748b; padding:16px;">No catalog snapshot available.</td></tr>');
            $('#catalogResultsCount').text('0 lists found');
            return;
        }

        // Update publishers dropdown if needed
        var pubSelect = $('#catalogPublisherSelect');
        if (pubSelect.children('option').length <= 1 && resp.publishers && resp.publishers.length > 0) {
            var currentVal = pubSelect.val();
            var optionsHtml = '<option value="">All Publishers</option>';
            resp.publishers.forEach(function(pub) {
                optionsHtml += '<option value="' + pub + '">' + pub + '</option>';
            });
            pubSelect.html(optionsHtml);
            if (currentVal) pubSelect.val(currentVal);
        }

        $('#catalogResultsCount').text(resp.total_matches + ' lists found' + (resp.total_matches > resp.returned_count ? ' (showing top ' + resp.returned_count + ')' : ''));

        if (resp.entries.length === 0) {
            $('#catalogTableBody').html('<tr><td colspan="4" style="text-align:center; color:#64748b; padding:16px;">No reading lists match your search filter.</td></tr>');
            return;
        }

        var rowsHtml = '';
        resp.entries.forEach(function(entry) {
            var eraBadge = entry.era ? ' <span style="color:#a78bfa; font-size:10px;">(' + entry.era + ')</span>' : '';
            var curationBadge = entry.curation ? ' <span style="color:#38bdf8; font-size:10px;">[' + entry.curation + ']</span>' : '';

            rowsHtml += '<tr>' +
                '<td>' +
                    '<strong>' + entry.clean_title + '</strong>' + eraBadge + curationBadge +
                    '<br><span style="font-size:10px; color:#64748b; font-family:monospace;">' + entry.path + '</span>' +
                '</td>' +
                '<td><span style="font-size:11px; font-weight:600; color:#e2e8f0;">' + entry.publisher + '</span></td>' +
                '<td><span class="badge-status badge-status-monitored" style="font-size:10px;">' + entry.category + '</span></td>' +
                '<td style="text-align:right;">' +
                    '<button type="button" class="btn-primary btn-sm" onclick="previewCatalogEntry(\'' + entry.id + '\')">Preview &amp; Reconcile</button>' +
                '</td>' +
            '</tr>';
        });

        $('#catalogTableBody').html(rowsHtml);
    }

    function backToCatalogSearch() {
        currentToken = null;
        $('#manifestMetaBox').hide();
        $('#previewResultsContainer').hide();
        $('#modalAlertBox').hide();
        $('#confirmImportBtn').hide();
        $('#catalogBackBtn').hide();
        $('#catalogSearchControls').show();
    }

    function previewCatalogEntry(entryId) {
        if (!entryId) return;

        $('#modalAlertBox').hide();
        $('#previewResultsContainer').hide();
        $('#confirmImportBtn').hide();
        $('#manifestMetaBox').hide();
        $('#catalogSearchControls').hide();

        // Show inline loading state
        $('#modalAlertBox')
            .removeClass('modal-alert-error modal-alert-success modal-alert-warning')
            .addClass('modal-alert-info')
            .html('Fetching raw manifest from GitHub and reconciling with local library...')
            .show();

        $.ajax({
            url: 'cbl_catalog_preview',
            type: 'GET',
            data: { entry_id: entryId },
            dataType: 'json',
            success: function(resp) {
                if (resp.status === 'success') {
                    currentToken = resp.upload_token;
                    $('#metaSourceFile').text(resp.filename);
                    $('#metaRepo').html('<a href="' + resp.repo_url + '" target="_blank" rel="noopener noreferrer" style="color:#60a5fa; text-decoration:underline;">DieselTech/CBL-ReadingLists</a> @ <code style="color:#a78bfa;">' + resp.repo_commit_short + '</code> (' + resp.repo_path + ')');
                    $('#metaRepoRow').show();
                    $('#metaHash').text(resp.sha256);
                    $('#catalogBackBtn').show();
                    $('#manifestMetaBox').show();
                    renderPreviewResults(resp);
                } else {
                    $('#catalogSearchControls').show();
                    $('#modalAlertBox')
                        .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                        .addClass('modal-alert-error')
                        .text(resp.message || 'Failed to preview catalog entry.')
                        .show();
                }
            },
            error: function() {
                $('#catalogSearchControls').show();
                $('#modalAlertBox')
                    .removeClass('modal-alert-success modal-alert-warning modal-alert-info')
                    .addClass('modal-alert-error')
                    .text('Network error retrieving catalog reading list.')
                    .show();
            }
        });
    }

    // ==========================================
    // Story Arc Detail Page Controls
    // ==========================================

    function switchArcView(mode) {
        var arcId = pageConfig.storyarcid || $('#page_storyarcid').val();
        if (mode === 'shelf') {
            $('#arcListView').hide();
            $('#arcShelfView').show();
            $('#viewModeListBtn').removeClass('active');
            $('#viewModeShelfBtn').addClass('active');
        } else {
            $('#arcShelfView').hide();
            $('#arcListView').show();
            $('#viewModeShelfBtn').removeClass('active');
            $('#viewModeListBtn').addClass('active');
        }
        if (arcId) {
            try {
                localStorage.setItem('mylar_arc_view_' + arcId, mode);
            } catch (e) {}
        }
    }

    function openDeleteArcModal() {
        $('#deleteModalAlert').hide();
        $('#deleteArcModal').fadeIn(150);
    }

    function closeDeleteArcModal() {
        $('#deleteArcModal').fadeOut(150);
    }

    function confirmDeleteStoryArc() {
        var arcId = pageConfig.storyarcid || $('#page_storyarcid').val() || '';
        if (!arcId) return;
        $('#confirmDeleteArcBtn').prop('disabled', true).text('Deleting...');

        $.ajax({
            url: 'cbl_delete_arc',
            type: 'POST',
            data: { storyarcid: arcId },
            dataType: 'json',
            success: function(resp) {
                if (resp.status === 'success') {
                    window.location.href = 'storyarc_main';
                } else {
                    $('#confirmDeleteArcBtn').prop('disabled', false).text('Delete Story Arc');
                    $('#deleteModalAlert')
                        .removeClass('modal-alert-success')
                        .addClass('modal-alert-error')
                        .text(resp.message || 'Failed to delete Story Arc.')
                        .show();
                }
            },
            error: function() {
                $('#confirmDeleteArcBtn').prop('disabled', false).text('Delete Story Arc');
                $('#deleteModalAlert')
                    .removeClass('modal-alert-success')
                    .addClass('modal-alert-error')
                    .text('Error communicating with server.')
                    .show();
            }
        });
    }

    $(document).ready(function() {
        initConfig();

        var arcId = pageConfig.storyarcid || $('#page_storyarcid').val();
        if (arcId && ($('#arcListView').length || $('#arcShelfView').length)) {
            var savedView = 'list';
            try {
                savedView = localStorage.getItem('mylar_arc_view_' + arcId) || 'list';
            } catch (e) {}
            switchArcView(savedView);
        }

        var dropzone = document.getElementById('cblDropzone');
        if (dropzone) {
            dropzone.addEventListener('dragover', function(e) {
                e.preventDefault();
                e.stopPropagation();
                $('#cblDropzone').addClass('dragover');
            });
            dropzone.addEventListener('dragleave', function(e) {
                e.preventDefault();
                e.stopPropagation();
                $('#cblDropzone').removeClass('dragover');
            });
            dropzone.addEventListener('drop', function(e) {
                e.preventDefault();
                e.stopPropagation();
                $('#cblDropzone').removeClass('dragover');
                if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                    handleFileSelected(e.dataTransfer.files);
                }
            });
        }
    });

    window.switchImportTab = switchImportTab;
    window.updateManifestMeta = updateManifestMeta;
    window.openCblModal = openCblModal;
    window.closeCblModal = closeCblModal;
    window.handleFileSelected = handleFileSelected;
    window.triggerCblFilePicker = triggerCblFilePicker;
    window.fetchCblPreview = fetchCblPreview;
    window.confirmCblImport = confirmCblImport;
    window.loadCatalogStatus = loadCatalogStatus;
    window.refreshDieselTechCatalog = refreshDieselTechCatalog;
    window.handleCatalogSearchInput = handleCatalogSearchInput;
    window.previewCatalogEntry = previewCatalogEntry;
    window.backToCatalogSearch = backToCatalogSearch;
    window.switchArcView = switchArcView;
    window.openDeleteArcModal = openDeleteArcModal;
    window.closeDeleteArcModal = closeDeleteArcModal;
    window.confirmDeleteStoryArc = confirmDeleteStoryArc;

})(window, jQuery);
